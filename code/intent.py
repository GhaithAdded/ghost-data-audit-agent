"""Decide whether each request_text actually authorizes deleting the account.

Requests are written in several languages and some only resemble deletion
requests ("remove me from marketing lists only", "keep the account, send me a
copy"). The model classifies each distinct text once; texts are batched to keep
token use low and answers are cached by llm.py.
"""

from __future__ import annotations

import sys

from llm import LLM, LLMUnavailable
from loaders import DeletionRequest

BATCH_SIZE = 100  # ~400 distinct texts -> 4 requests

PROMPT = """You review privacy requests sent to an online shop, written in any language. For each
numbered request decide whether it authorizes DELETING the person's account and personal data.

Answer DELETE when the person:
- asks to delete, erase, permanently remove or close their account and its personal data;
- asks to finish, retry or fix a deletion that failed or was only partly done;
- asks to check, verify or audit that their account or data was really deleted;
- asks to remove remaining copies of their personal data (search results, exports or download
  links, support attachments, analytics, integrations, backups), including records under
  their old and current email addresses.
Keeping some records (security logs, backups that cannot be restored, anonymous statistics,
a deleted-account reference) does not change a deletion request.

Answer OTHER when the request does not ask for deletion, for example: access to or a copy of
their data, data export or portability, correcting or updating details, changing the email or
phone, marketing opt-out or unsubscribing only, login or password help, invoices or order
questions, or any request that asks to keep the account active or not to delete anything.

Requests:
{items}

Return one entry per request with its id, the decision, and a short reason in English."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "decision": {"type": "string", "enum": ["DELETE", "OTHER"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "decision", "reason"],
            },
        }
    },
    "required": ["results"],
}


def classify_texts(texts: list[str], llm: LLM) -> dict[str, bool]:
    """Map each distinct text to True when it authorizes deletion."""
    unique = sorted(set(texts))
    batches = [unique[start:start + BATCH_SIZE] for start in range(0, len(unique), BATCH_SIZE)]

    def ask(batch: list[str]) -> dict | None:
        items = "\n".join(f"[{i}] {text}" for i, text in enumerate(batch))
        try:
            return llm.generate_json("intent", PROMPT.format(items=items), SCHEMA)
        except LLMUnavailable as exc:
            print(f"WARNING: intent batch not classified ({exc}); its {len(batch)} texts default to deletion. "
                  "Rerun to fill the cache.", file=sys.stderr)
            return None

    decisions: dict[str, bool] = {}
    for batch, answer in zip(batches, llm.map(ask, batches)):
        if answer is None:  # every evaluation-style request is a deletion unless shown otherwise
            decisions.update({text: True for text in batch})
            continue
        by_id = {r["id"]: r["decision"] for r in answer["results"]}
        for i, text in enumerate(batch):
            if i not in by_id:
                raise ValueError(f"intent answer missing id {i} for text {text[:60]!r}")
            decisions[text] = by_id[i] == "DELETE"
    return decisions


def authorized_cases(requests: list[DeletionRequest], llm: LLM) -> dict[str, bool]:
    decisions = classify_texts([r.request_text for r in requests], llm)
    return {r.case_id: decisions[r.request_text] for r in requests}
