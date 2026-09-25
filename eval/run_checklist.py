"""Eval 3 (PromptLint.md §12): per-check precision and recall against hand labels.

    python eval/run_checklist.py
A check "fires" when Jev's probability is ≥ 0.5, matching how the app shows pass/fail.
"""

import asyncio

from common import judge_all, load_jsonl, update_summary, write_result

CHECKS = [
    "has_output_format",
    "has_constraints",
    "has_audience",
    "has_examples",
    "conflicting",
    "multi_task",
    "needs_current_info",
]


async def main() -> None:
    rows = load_jsonl("checklist_labels.jsonl")
    answers, latencies = await judge_all([r["prompt"] for r in rows], label="checklist")

    per_check = {}
    total = agree = 0
    disagreements = []
    for check in CHECKS:
        tp = fp = fn = tn = 0
        for r in rows:
            p = answers[r["prompt"]].nouls[check]
            pred, gold = p >= 0.5, bool(r["labels"][check])
            tp += pred and gold
            fp += pred and not gold
            fn += gold and not pred
            tn += not pred and not gold
            if pred != gold:
                disagreements.append(
                    {
                        "id": r["id"],
                        "check": check,
                        "label": gold,
                        "probability": round(p, 3),
                        "prompt": r["prompt"][:120],
                    }
                )
        n = tp + fp + fn + tn
        per_check[check] = {
            "accuracy": (tp + tn) / n,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "positives": tp + fn,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
        }
        total += n
        agree += tp + tn

    result = {
        "n_prompts": len(rows),
        "n_labels": total,
        "accuracy": agree / total,
        "per_check": per_check,
        "disagreements": disagreements,
    }
    write_result("checklist", result)
    update_summary("checklist", {"accuracy": result["accuracy"], "n": total}, latencies=latencies)

    print(f"\nChecklist agreement: {agree}/{total} = {agree / total:.1%}")
    print(f"{'check':20s} {'acc':>6s} {'prec':>6s} {'recall':>7s}  positives")
    for c, m in per_check.items():
        fmt = lambda x: "  n/a" if x is None else f"{x:6.0%}"  # noqa: E731
        print(f"{c:20s} {m['accuracy']:6.0%} {fmt(m['precision'])} {fmt(m['recall']):>7s}  {m['positives']}")


if __name__ == "__main__":
    asyncio.run(main())
