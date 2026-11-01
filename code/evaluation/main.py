"""Score predictions against expected answers (by default: the 32 public samples).

Run from the repository root:
    python code/evaluation/main.py                  # runs the agent on the samples, then scores
    python code/evaluation/main.py --details        # also prints every differing case

Metrics
- case status accuracy: deletion_status matches.
- step precision / recall / F1: steps compared as (phase, system_id, resource_id,
  operation, state, evidence_id), ignoring step_no.
- field accuracy: for each field, share of expected steps whose same-position
  predicted step has the same value.
- sequence accuracy: the whole ordered steps_json matches exactly.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders import DATASET_DIR  # noqa: E402

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def read_answers(path: Path) -> dict[str, tuple[str, list[dict]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["case_id"]: (row["deletion_status"], json.loads(row["steps_json"]))
                for row in csv.DictReader(handle)}


def as_tuple(step: dict) -> tuple:
    return tuple(step[f] for f in FIELDS)


def score(expected: dict, predicted: dict) -> dict:
    status_ok = tp = n_pred = n_exp = seq_ok = 0
    field_ok = Counter()
    for case_id, (exp_status, exp_steps) in expected.items():
        pred_status, pred_steps = predicted.get(case_id, ("", []))
        status_ok += pred_status == exp_status
        exp_t, pred_t = Counter(map(as_tuple, exp_steps)), Counter(map(as_tuple, pred_steps))
        tp += sum((exp_t & pred_t).values())
        n_exp += len(exp_steps)
        n_pred += len(pred_steps)
        seq_ok += [as_tuple(s) for s in exp_steps] == [as_tuple(s) for s in pred_steps]
        for i, step in enumerate(exp_steps):
            if i < len(pred_steps):
                for f in FIELDS:
                    field_ok[f] += pred_steps[i][f] == step[f]
    n = len(expected)
    precision = tp / n_pred if n_pred else 0.0
    recall = tp / n_exp if n_exp else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": {f: field_ok[f] / n_exp for f in FIELDS},
        "sequence_accuracy": seq_ok / n,
        "expected_steps": n_exp,
        "predicted_steps": n_pred,
    }


def print_report(metrics: dict) -> None:
    print(f"cases               {metrics['cases']}")
    print(f"status accuracy     {metrics['status_accuracy']:.1%}")
    print(f"step precision      {metrics['step_precision']:.1%}  ({metrics['predicted_steps']} predicted)")
    print(f"step recall         {metrics['step_recall']:.1%}  ({metrics['expected_steps']} expected)")
    print(f"step F1             {metrics['step_f1']:.1%}")
    print(f"sequence accuracy   {metrics['sequence_accuracy']:.1%}")
    print("field accuracy      " + "  ".join(f"{f}={v:.1%}" for f, v in metrics["field_accuracy"].items()))


def print_details(expected: dict, predicted: dict) -> None:
    for case_id, (exp_status, exp_steps) in expected.items():
        pred_status, pred_steps = predicted.get(case_id, ("", []))
        exp_t, pred_t = Counter(map(as_tuple, exp_steps)), Counter(map(as_tuple, pred_steps))
        missing, extra = exp_t - pred_t, pred_t - exp_t
        order_only = not missing and not extra and [as_tuple(s) for s in exp_steps] != [as_tuple(s) for s in pred_steps]
        if exp_status == pred_status and not missing and not extra and not order_only:
            continue
        print(f"\n{case_id}: expected {exp_status}, predicted {pred_status}{'  (order differs)' if order_only else ''}")
        for t in missing.elements():
            print("   - missing ", " | ".join(t))
        for t in extra.elements():
            print("   + extra   ", " | ".join(t))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expected", type=Path, default=DATASET_DIR / "sample_output.csv")
    parser.add_argument("--predicted", type=Path, help="predictions CSV; default: run the agent on the samples")
    parser.add_argument("--details", action="store_true", help="print every differing case")
    args = parser.parse_args()

    if args.predicted is None:
        from main import SAMPLE_PREDICTIONS, run, write_output
        write_output(run(samples=True, verbose=False), SAMPLE_PREDICTIONS)
        args.predicted = SAMPLE_PREDICTIONS
    expected, predicted = read_answers(args.expected), read_answers(args.predicted)
    print_report(score(expected, predicted))
    if args.details:
        print_details(expected, predicted)


if __name__ == "__main__":
    main()
