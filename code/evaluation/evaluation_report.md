# Evaluation report — Ghost Data Still Exists

All numbers come from the final code and the cached model answers used for the final
`output.csv`. Reproduce them from the repository root:

```bash
python code/evaluation/main.py --details   # 32 public samples vs dataset/sample_output.csv
python code/evaluation/custom_cases.py     # additional cases created for this solution
```

## 1. Results on the 32 public samples

| Metric | Score |
|---|---|
| Case-level deletion-status accuracy | **100.0%** (32/32) |
| Step precision (predicted steps that are correct) | **99.5%** (182/183) |
| Step recall (expected steps produced) | **94.3%** (182/193) |
| Step F1 | **96.8%** |
| Sequence accuracy (whole ordered `steps_json` exactly right) | **68.8%** (22/32) |

Field-level accuracy (for each expected step, the predicted step at the same position):

| phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|
| 94.8% | 89.6% | 89.1% | 89.6% | 89.6% | 94.8% |

How the metrics are computed (`code/evaluation/main.py`): a step matches when all of
`phase, system_id, resource_id, operation, state, evidence_id` are equal (`step_no` is ignored
because it follows from the order). Precision/recall count matching steps as multisets per case.
Field accuracy compares position by position, so one missing step early in a case also lowers
the fields of the steps after it. Sequence accuracy requires the full ordered list to match.

### Remaining differences and their causes

Every remaining difference is a record the agent could not tie to the person from
participant-visible data; none comes from policy, ordering or serialization.

| Case | Difference | Cause |
|---|---|---|
| case_03, 21, 25, 26, 27, 28 | missing `VERIFY_RECORD_ANONYMIZED` for an analytics record | After anonymization the record holds no identifier. The only link is the deletion worker's `ANONYMIZE_RECORD` shortly before `reported_at`, and several requests audited at the same moment compete for the same records. The agent assigns a record only when exactly one request remains plausible (see 4.3). |
| case_04 | missing account and analytics checks | Hand-written sample with no participant-visible link to either resource. |
| case_18, case_20 | missing `VERIFY_RECORD_PRESENT` for a security audit record | The record appears in no evidence file, screenshot or activity. |
| case_23 | `account_7015` instead of `account_410` | Three identical `FAILED` account deletions fall in the same window for three requests with the same status; the choice is a tie. |

## 2. Additional evaluation cases created for this solution

Metamorphic cases: each takes a public sample with a known answer, changes one property of
the request, and states what the answer must then be (`code/evaluation/custom_cases.py`).

| # | Variant | Capability tested | Expected | Result |
|---|---|---|---|---|
| C1 | sample case_10 filed with the subject's **current** email (`n.petrescu@yahoo.com`) instead of the historical one | identity resolution across email changes | identical status and steps to case_10 | **PASS** |
| C2 | sample case_32 with the email in **upper case** | case-insensitive identifier matching | identical to case_32 | **PASS** |
| C3 | case_01's subject sends *"Quiero descargar una copia de mis datos personales. Por favor, no eliminen mi cuenta; sigo usándola."* | multilingual intent detection | `NOT_APPLICABLE`, `[]` | **PASS** |
| C4 | sample case_16 with `audit_at` one minute **before** its key evidence (`evidence_05`) was captured | the `audit_at` cutoff | `evidence_05` never cited; no citation captured after `audit_at` | **PASS** (cites only `evidence_02`) |

**4/4 custom cases pass.**

## 3. Checks on the 518 evaluation requests (no answers available)

- **Output contract** (validated on the final `output.csv`): header and 518 rows sorted by case
  number; valid JSON; consecutive `step_no`; legal phase/operation/state combinations; phase and
  operation-table order; every finding cites evidence; every `COMPLETE` step cites evidence;
  `INCOMPLETE` remediation/verification steps cite nothing; no citation captured after the
  case's `audit_at`; `NOT_APPLICABLE` rows have `[]`. **0 violations.**
- **Determinism**: two runs from the cache produce byte-identical `output.csv` (same SHA-256).
- **Status distribution**: 357 INCOMPLETE, 151 COMPLETE, 10 NOT_APPLICABLE; 3,169 steps.
- **Intent review**: the 10 NOT_APPLICABLE requests were read one by one: phone update, stop
  promotional emails, data download, login help, surname correction, marketing opt-out only,
  information access, invoice copy, email change, copy of data. All are non-deletion requests,
  and none of the 32 samples (all deletions) was flagged.
- **Screenshot review**: the extracted records were compared with the 11 screenshots by eye
  (customer page, search hits, connectors, analytics profile, exports, support ticket, backups,
  security events, marketing profile); resource IDs, states and identifiers matched.

## 4. Failures found and improvements made

Measured on the samples unless stated otherwise; each change was kept only if it did not lower
the other metrics.

1. **Deleted accounts carry no identifier.** 16 of 32 sample accounts could not be linked from
   evidence. Analysis showed the deletion worker deletes each request's account 45–75 minutes
   before `reported_at`, and its reported result mirrors the workflow status
   (`COMPLETE`↔`SUCCESS`, `PARTIALLY_COMPLETE`↔`PARTIAL`, `NOT_STARTED`↔`FAILED`). A global
   one-to-one matching over all 550 requests using timing, that status rule and evidence ownership
   raised uniquely matched requests from 300 to 414, with 0 wrong sample matches.
2. **Decoy profile lookups.** 300 cloud-log lookups by a `profile_service` reported 5-digit
   accounts tied to real users' emails that exist nowhere else; they are excluded as
   non-authoritative. Names are never used alone to link records (several users share a name).
3. **Anonymized analytics ties.** Assigning tied records in a fixed order gave recall 92.8% and
   precision 92.8%; assigning only when one request remains plausible gave recall 90.1% and
   precision 97.1% (better F1), so ties are left unassigned. Claimants are also limited to
   requests audited right after a file listing the record.
4. **People who filed two requests.** Records from the earlier request's episode (last tied to
   the person before that audit and gone or not seen since) leaked into the later case.
   Extra resources fell from 8 to 1.
5. **Citations.** A file that names the record as missing (a 404 lookup) is preferred over a
   later listing that merely omits it, and a listing of the primary database cannot prove a copy
   in a replica gone. Citation agreement went from 61/67 to 67/67.
6. **Omission versus retained records.** Partial security listings made retained audit records
   look deleted. Omission now overrides an explicit presence only if a delete/anonymize/redact
   action happened in between, and never for security or legal records. State verdicts went from
   96/100 to 100/100 consistent with the answer key.
7. **Later evidence changing the answer.** The contract validator found 17 findings with nothing
   citable: their state came from files exported after `audit_at`. State is now decided from
   files captured by `audit_at` whenever any exist; later files only reveal that a retained
   record exists. Findings without evidence: 17 → 0; COMPLETE steps without evidence: 3 → 0.
8. **Intent prompt too narrow.** The first prompt classified "please verify my deletion" and
   "remove records under both my addresses" as non-deletion, contradicting the samples. The
   prompt now treats verifying, finishing or cleaning up a deletion as deletion. Samples wrongly
   flagged: 0.
9. **Model quota and outages.** The free tier allows 20 requests per day, and failed (503/429)
   attempts also count; parallel retries exhausted it before any answer arrived. Calls were cut
   from ~27 to 7 per full run (100 texts and 4 screenshots per request), made sequential with
   3 attempts, every answer is cached, and a persistent daily budget stops the run before the
   limit. Without the model the pipeline still writes a valid file (unclassified texts count as
   deletions, unread screenshots stay pending).
10. **A deletion case with no steps (case_61).** The person's only record was an attachment
    confirmed by a data-catalog activity (`object_19`, prefix not yet mapped). Catalog
    activities name each resource's document alias (a file name or statement number); a
    citable document carrying the alias and naming the person now proves existence, and the
    catalog activity itself is used only when no file mentions the record. A same-named
    attachment of another customer (`delivery_confirmation_19.pdf`) and identifier aliases
    (`user_17`) are rejected. Sample scores unchanged; 3 evaluation cases corrected.

## 5. Known limitations

- Ties among requests audited at the same moment (anonymized analytics, a few accounts) cannot be
  resolved from participant data; the agent prefers omission to guessing.
- Activity results are treated as claims; only a catalog confirmation is used as proof of existence,
  and only when no evidence file mentions the record.
- Screenshot extraction depends on the model reading the page correctly; the extraction was checked
  by hand for these 11 images.
