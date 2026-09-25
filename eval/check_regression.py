"""Fail (exit 1) if the latest eval results fall below the committed floors.

Run after run_pairwise.py and run_checklist.py. CI runs this whenever backend/config/ changes, so a
wording or weight tweak can't quietly make the scores worse (PromptLint.md §13, "Regression").
Raise a floor only after a change has been shown to lift the metric.
"""

import json
import sys

from common import RESULTS

FLOORS = {
    ("pairwise", "main", "accuracy"): 0.95,
    ("pairwise", "hard", "accuracy"): 0.85,
    ("checklist", "accuracy"): 0.93,
}


def get(path: tuple[str, ...]) -> float:
    data = json.loads((RESULTS / f"{path[0]}.json").read_text())
    for key in path[1:]:
        data = data[key]
    return float(data)


def main() -> None:
    failed = False
    for path, floor in FLOORS.items():
        value = get(path)
        ok = value >= floor
        failed |= not ok
        print(f"{'PASS' if ok else 'FAIL'}  {'.'.join(path):28s} {value:.3f}  (floor {floor:.2f})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
