# Usage report — model calls, tokens and cost

## Final full-dataset run (produced `output.csv`)

| Item | Value |
|---|---|
| Provider | Google Gemini API (free tier) |
| Model | `gemini-3.5-flash` |
| Requests processed | 518 |
| Model calls | **7** (4 intent, 3 screenshot) |
| Date of the live calls | 2026-10-31 |

The agent uses the model only for two tasks; everything else is deterministic code.

| Task | Calls | What one call does | Input tokens | Output tokens | Thinking tokens | Total tokens |
|---|---|---|---|---|---|---|
| Request intent | 4 | classifies up to 100 distinct request texts | 17,014 | 15,122 | 0 | 32,136 |
| Screenshot reading | 3 | extracts records from up to 4 screenshots | 13,210 | 2,137 | 0 | 15,347 |
| **Total** | **7** | | **30,224** | **17,259** | **0** | **47,483** |

| Average | Value |
|---|---|
| Tokens per request (47,483 / 518) | **91.7** (58.3 input, 33.3 output) |
| Tokens per model call (47,483 / 7) | 6,783 |
| Model calls per request | 0.014 |

The answers of these 7 calls are cached (`code/cache/llm_responses.json`). The final
regeneration of `output.csv` reused them with **0 new calls**, and two cached runs produce
byte-identical output.

## Cost

| | Input | Output | Total | Per request |
|---|---|---|---|---|
| Actual cost (free tier) | $0.00 | $0.00 | **$0.00** | $0.00 |
| Paid-tier estimate | $0.0227 | $0.0647 | **$0.0874** | **$0.00017** |

Estimate basis: $0.75 per 1M input tokens and $3.75 per 1M output tokens (thinking tokens are
billed as output; none were used). These are the paid-tier list prices of Gemini 3.6 Flash and
3.8 Flash on the Gemini pricing page (checked 2026-10-31, valid through 2026-12-31);
Gemini 3.5 Flash is no longer listed, so the price of its nearest listed successors is used.

## Per-model and overall totals (all model calls during development and evaluation)

| Model | Purpose | Calls | Input | Output | Total tokens | Paid-tier estimate |
|---|---|---|---|---|---|---|
| `gemini-3.5-flash` | final run (above) | 7 | 30,224 | 17,259 | 47,483 | $0.0874 |
| `gemini-3.5-flash` | custom evaluation cases (intent for the new request text) | 4 | 17,008 | 15,386 | 32,394 | $0.0704 |
| **`gemini-3.5-flash` total** | | **11** | **47,232** | **32,645** | **79,877** | **$0.1578** |
| `gemini-3.8-flash` | first development run with an earlier intent prompt (answers not used) | 11 | 13,213 | 9,591 | 22,804 | $0.0459 |
| **Overall** | | **22** | **60,445** | **42,236** | **102,681** | **$0.2037** |

Calls are counted only when they returned an answer. Failed attempts (503 "high demand" and
429 quota errors) returned no tokens; they consumed free-tier request quota but no billable
tokens.

## Token efficiency

- The model handles only judgement on unstructured input (intent in 6 languages, screenshots);
  identity resolution, evidence parsing, state reconstruction and policy are deterministic.
- 518 requests contain only 398 distinct texts; each distinct text is classified once, 100 per call.
- Four screenshots are read per call; PDFs are read with `pypdf` without a model.
- `thinking_level="low"` and a JSON response schema keep output short; 0 thinking tokens were used.
- Every answer is cached by model, task, prompt and image bytes, so reruns cost nothing, and a
  persistent daily request budget keeps the agent within the free-tier limit.
