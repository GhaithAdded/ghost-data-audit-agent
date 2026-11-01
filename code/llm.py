"""Gemini client with a response cache and token accounting.

The agent uses a model only for two judgement tasks (request intent and reading
screenshots); everything else is deterministic code. Every answer is cached in
code/cache/llm_responses.json keyed by model + task + prompt (+ image bytes), so:
- each answer is paid for once (the free tier allows only 20 requests/day/model);
- a rerun makes no API calls and returns identical answers (deterministic output);
- the run works without an API key once the cache is filled;
- token usage of the calls behind each cached answer is kept for usage_report.md.

The API key is read from the GEMINI_API_KEY environment variable (or an
uncommitted .env file at the repository root). It is never logged or stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
CACHE_PATH = CODE_DIR / "cache" / "llm_responses.json"
USAGE_PATH = CODE_DIR / "cache" / "usage_last_run.json"
BUDGET_PATH = CODE_DIR / "cache" / "request_budget.json"
# Free tier: 20 requests per day (reset at midnight US Pacific), and failed attempts count
# too, so the tasks are batched into a handful of calls and every attempt is budgeted.
DEFAULT_MODEL = "gemini-3.5-flash"
PARALLEL_CALLS = int(os.environ.get("LLM_PARALLEL_CALLS", "1"))
RETRIES = 3
DAILY_BUDGET = int(os.environ.get("LLM_DAILY_BUDGET", "18"))  # stay below the 20/day limit


def _quota_day() -> str:
    """The quota day: the date at US Pacific time, when the free-tier quota resets."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/Los_Angeles")
    except Exception:  # no tz database (e.g. Windows without tzdata): use PST
        zone = timezone(timedelta(hours=-8))
    return datetime.now(zone).date().isoformat()


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str | None = None) -> None:
        try:
            from dotenv import load_dotenv
            load_dotenv(CODE_DIR.parent / ".env")
        except ImportError:
            pass
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self._client = None
        self.cache: dict = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.is_file() else {}
        self._lock = threading.Lock()
        # per task: answers used, live API calls, and the tokens behind the answers used
        self.usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def _spend_attempt(self) -> None:
        """Count one API attempt against today's budget, shared by every run and model."""
        with self._lock:
            day = _quota_day()
            spent = json.loads(BUDGET_PATH.read_text(encoding="utf-8")) if BUDGET_PATH.is_file() else {}
            if spent.get(day, 0) >= DAILY_BUDGET:
                raise LLMUnavailable(f"today's budget of {DAILY_BUDGET} requests is used; "
                                     "rerun after midnight US Pacific to continue from the cache")
            spent[day] = spent.get(day, 0) + 1
            BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
            BUDGET_PATH.write_text(json.dumps(spent, indent=1, sort_keys=True), encoding="utf-8")

    def budget_left(self) -> int:
        spent = json.loads(BUDGET_PATH.read_text(encoding="utf-8")) if BUDGET_PATH.is_file() else {}
        return max(0, DAILY_BUDGET - spent.get(_quota_day(), 0))

    def _client_or_raise(self):
        if self._client is None:
            if not os.environ.get("GEMINI_API_KEY"):
                raise LLMUnavailable("GEMINI_API_KEY is not set and the answer is not cached")
            from google import genai
            self._client = genai.Client()
        return self._client

    def generate_json(self, task: str, prompt: str, schema: dict, images: list[Path] = ()) -> dict | list:
        """Return the model's JSON answer for prompt (and optional images), using the cache."""
        image_bytes = [image.read_bytes() for image in images]
        key = hashlib.sha256(json.dumps([self.model, task, prompt] + [hashlib.sha256(b).hexdigest()
                                                                      for b in image_bytes])
                             .encode("utf-8")).hexdigest()
        with self._lock:
            entry = self.cache.get(key)
        if entry is None:  # the API call runs outside the lock so calls can overlap
            entry = self._call(prompt, schema, image_bytes)
            entry["task"] = task
            with self._lock:
                self.cache[key] = entry
                self._save_cache()
                self.usage[task]["live_calls"] += 1
        with self._lock:
            stats = self.usage[task]
            stats["answers"] += 1
            for field in ("input_tokens", "output_tokens", "thinking_tokens"):
                stats[field] += entry["usage"][field]
        return entry["response"]

    def map(self, function, items: list) -> list:
        """Apply function to items concurrently (model latency dominates), keeping input order."""
        with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
            return list(pool.map(function, items))

    def _call(self, prompt: str, schema: dict, image_bytes: list[bytes]) -> dict:
        from google.genai import errors, types

        client = self._client_or_raise()
        parts = [types.Part.from_text(text=prompt)]
        parts += [types.Part.from_bytes(data=data, mime_type="image/png") for data in image_bytes]
        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )
        for attempt in range(RETRIES):
            self._spend_attempt()
            try:
                response = client.models.generate_content(
                    model=self.model, contents=[types.Content(role="user", parts=parts)], config=config)
                break
            except errors.APIError as exc:  # rate limits and "model overloaded" are temporary
                if getattr(exc, "code", None) not in (429, 500, 502, 503, 504):
                    raise
                if "PerDay" in str(exc):  # a daily quota cannot recover by waiting minutes
                    raise LLMUnavailable("daily request quota exhausted for this model") from exc
                if attempt == RETRIES - 1:
                    raise LLMUnavailable(f"model still unavailable after {RETRIES} attempts: {exc.code}") from exc
                time.sleep(45 * (attempt + 1) + random.uniform(0, 5))
        meta = response.usage_metadata
        return {
            "model": self.model,
            "response": json.loads(response.text),
            "usage": {
                "input_tokens": meta.prompt_token_count or 0,
                "output_tokens": meta.candidates_token_count or 0,
                "thinking_tokens": meta.thoughts_token_count or 0,
            },
        }

    def _save_cache(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)

    def save_usage(self, requests: int) -> dict:
        """Write this run's usage summary (read by the usage report)."""
        summary = {"model": self.model, "requests": requests,
                   "tasks": {task: dict(stats) for task, stats in sorted(self.usage.items())}}
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        return summary
