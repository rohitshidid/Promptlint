"""Eval 3 (PromptLint.md §12): per-check precision and recall against hand labels.

    python eval/run_checklist.py
A check "fires" when Jev's probability is ≥ 0.5, matching how the app shows pass/fail.
"""

import asyncio

from common import answers_for, load_jsonl, update_summary, write_result

CHECKS = [
    "has_output_format",
    "has_constraints",
    "has_audience",
    "has_examples",
    "conflicting",
    "multi_task",
    "needs_current_info",
]


def evaluate(rows: list[dict], answers) -> dict:
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
    return {
        "n_prompts": len(rows),
        "n_labels": total,
        "accuracy": agree / total,
        "per_check": per_check,
        "disagreements": disagreements,
    }


async def main() -> None:
    rows = load_jsonl("checklist_labels.jsonl")
    results, latencies = {}, []
    for backend in ("jev", "heuristic"):
        answers, lat = await answers_for(backend, [r["prompt"] for r in rows], "checklist")
        latencies += lat
        results[backend] = evaluate(rows, answers)

    jev, heur = results["jev"], results["heuristic"]
    write_result("checklist", {**jev, "baseline_heuristic": heur})
    update_summary(
        "checklist",
        {"accuracy": jev["accuracy"], "n": jev["n_labels"], "heuristic_accuracy": heur["accuracy"]},
        latencies=latencies,
    )

    fmt = lambda x: "  n/a" if x is None else f"{x:6.0%}"  # noqa: E731
    print(
        f"\nChecklist agreement: Jev {jev['accuracy']:.1%} · heuristic baseline {heur['accuracy']:.1%} ({jev['n_labels']} labels)"
    )
    print(
        f"{'check':20s} {'jev acc':>8s} {'prec':>6s} {'recall':>7s} | {'heur acc':>8s} {'prec':>6s} {'recall':>7s}"
    )
    for c in CHECKS:
        j, h = jev["per_check"][c], heur["per_check"][c]
        print(
            f"{c:20s} {j['accuracy']:8.0%} {fmt(j['precision'])} {fmt(j['recall']):>7s} | "
            f"{h['accuracy']:8.0%} {fmt(h['precision'])} {fmt(h['recall']):>7s}"
        )


if __name__ == "__main__":
    asyncio.run(main())
