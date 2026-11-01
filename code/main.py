"""Ghost Data Still Exists - deletion-audit agent entry point.

Run from the repository root:
    python code/main.py                 # 518 evaluation requests -> ./output.csv
    python code/main.py --samples       # 32 sample requests -> code/evaluation/sample_predictions.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

from evidence import EvidenceStore
from identity import IdentityIndex
from intent import authorized_cases
from llm import LLM
from loaders import DATASET_DIR, OUTPUT_COLUMNS, REPO_ROOT, load_dataset, load_requests
from policy import Decision, decide
from state import StateBuilder
from vision import extract_screenshots

SAMPLE_PREDICTIONS = Path(__file__).resolve().parent / "evaluation" / "sample_predictions.csv"


def case_sort_key(case_id: str) -> tuple:
    """case_33 < case_100: compare the numeric suffix, not the text."""
    match = re.search(r"(\d+)$", case_id)
    return (case_id[: match.start()], int(match.group(1))) if match else (case_id, -1)


def write_output(decisions: list[Decision], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUTPUT_COLUMNS)
        for d in sorted(decisions, key=lambda d: case_sort_key(d.case_id)):
            writer.writerow([d.case_id, d.deletion_status, d.steps_json()])


def run(samples: bool, verbose: bool = True) -> list[Decision]:
    requests_file = "sample_deletion_requests.csv" if samples else "deletion_requests.csv"
    other_file = "deletion_requests.csv" if samples else "sample_deletion_requests.csv"
    data = load_dataset(requests_file=requests_file)
    # Sample and evaluation requests describe the same world, so both compete in the
    # one-to-one matching of deletion-worker activity to requests.
    all_requests = data.requests + load_requests(DATASET_DIR / other_file)

    llm = LLM()
    authorized = authorized_cases(all_requests, llm)  # model: does the text ask for deletion?
    screenshots = extract_screenshots(data, llm)  # model: read the PNG evidence
    index = IdentityIndex(data)
    store = EvidenceStore(data, screenshots)
    builder = StateBuilder(data, store, index, all_requests,
                           eligible={cid for cid, ok in authorized.items() if ok})

    decisions = [decide(builder.build(r), data.rules, authorized[r.case_id]) for r in data.requests]
    if not samples:
        llm.save_usage(len(data.requests))

    if verbose:
        print(f"requests        {len(data.requests)}  ({requests_file})")
        print(f"evidence        {len(store.observations)} observations, {len(store.pending)} screenshots pending")
        print(f"statuses        {dict(Counter(d.deletion_status for d in decisions))}")
        print(f"steps           {sum(len(d.steps) for d in decisions)}  "
              f"{dict(Counter(s.phase for d in decisions for s in d.steps))}")
        for task, stats in sorted(llm.usage.items()):
            print(f"llm {task:11} {stats['answers']} answers, {stats['live_calls']} live calls, "
                  f"{stats['input_tokens']} in / {stats['output_tokens']} out / {stats['thinking_tokens']} thinking tokens")
        print(f"llm budget      {llm.budget_left()} requests left today ({llm.model})")
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit deletion requests in dataset/.")
    parser.add_argument("--samples", action="store_true", help="run on sample_deletion_requests.csv")
    parser.add_argument("--output", type=Path, help="where to write the predictions CSV")
    args = parser.parse_args()

    decisions = run(args.samples)
    output = args.output or (SAMPLE_PREDICTIONS if args.samples else REPO_ROOT / "output.csv")
    write_output(decisions, output)
    print(f"wrote           {output}")


if __name__ == "__main__":
    main()
