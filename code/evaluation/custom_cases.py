"""Additional evaluation cases created for this solution (metamorphic tests).

Each case takes a public sample whose correct answer is known, changes one thing
about the request, and states what the answer must then be. The variant replaces
its base sample in the request pool, as if the sample had been filed that way.

Run from the repository root:
    python code/evaluation/custom_cases.py
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence import EvidenceStore  # noqa: E402
from identity import IdentityIndex  # noqa: E402
from intent import authorized_cases  # noqa: E402
from llm import LLM  # noqa: E402
from loaders import DATASET_DIR, load_dataset, load_requests  # noqa: E402
from policy import decide  # noqa: E402
from state import StateBuilder  # noqa: E402
from vision import extract_screenshots  # noqa: E402


def expected_sample(case_id: str) -> tuple[str, list[dict]]:
    with (DATASET_DIR / "sample_output.csv").open(encoding="utf-8", newline="") as handle:
        row = next(r for r in csv.DictReader(handle) if r["case_id"] == case_id)
    return row["deletion_status"], json.loads(row["steps_json"])


def main() -> None:
    data = load_dataset(requests_file="sample_deletion_requests.csv")
    samples = {r.case_id: r for r in data.requests}
    evaluation = load_requests(DATASET_DIR / "deletion_requests.csv")
    evidence_05 = data.evidence["evidence_05"].captured_at

    cases = [
        ("C1 historical -> current email", "case_10",
         replace(samples["case_10"], case_id="custom_01", email="n.petrescu@yahoo.com"), "same_as_base"),
        ("C2 upper-case email", "case_32",
         replace(samples["case_32"], case_id="custom_02", email=samples["case_32"].email.upper()), "same_as_base"),
        ("C3 non-deletion request (Spanish)", "case_01",
         replace(samples["case_01"], case_id="custom_03",
                 request_text="Quiero descargar una copia de mis datos personales. "
                              "Por favor, no eliminen mi cuenta; sigo usándola."), "not_applicable"),
        ("C4 audit one minute before the key evidence", "case_16",
         replace(samples["case_16"], case_id="custom_04", audit_at=evidence_05 - timedelta(minutes=1)),
         "no_late_evidence"),
    ]

    llm = LLM()
    store = EvidenceStore(data, extract_screenshots(data, llm))
    index = IdentityIndex(data)
    passed = 0
    for name, base_id, variant, check in cases:
        pool = [variant if r.case_id == base_id else r for r in data.requests] + evaluation
        authorized = authorized_cases(pool, llm)
        builder = StateBuilder(data, store, index, pool, eligible={c for c, ok in authorized.items() if ok})
        decision = decide(builder.build(variant), data.rules, authorized[variant.case_id])
        steps = json.loads(decision.steps_json())

        if check == "same_as_base":
            want_status, want_steps = expected_sample(base_id)
            ok = decision.deletion_status == want_status and steps == want_steps
            detail = f"expected {want_status} with {len(want_steps)} steps of {base_id}"
        elif check == "not_applicable":
            ok = decision.deletion_status == "NOT_APPLICABLE" and steps == []
            detail = "expected NOT_APPLICABLE with []"
        else:
            cited = {s["evidence_id"] for s in steps if s["evidence_id"]}
            late = [e for e in cited if data.evidence[e].captured_at > variant.audit_at]
            ok = not late and "evidence_05" not in cited
            detail = f"cited {sorted(cited) or 'nothing'}; late citations {late or 'none'}"
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:46} -> {decision.deletion_status:14} {len(steps):2} steps | {detail}")
    print(f"\n{passed}/{len(cases)} custom cases passed")


if __name__ == "__main__":
    main()
