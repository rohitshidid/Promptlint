"""Eval 1 (PromptLint.md §12, PQS §10): does the score rank the improved prompt above the weak one?

    python eval/run_pairwise.py            # uses cached Jev answers where available

Two sets, reported separately:
  prompt_pairs.jsonl       200 pairs, vague vs well-specified (target ≥ 85%)
  prompt_pairs_hard.jsonl   40 near-miss pairs: a decent prompt vs the same prompt plus ONE missing piece
Each is scored four ways: backend (jev | heuristic baseline) × score (lint_score | pqs_score).
Ties count as failures.
"""

import asyncio
import collections
import csv

from common import (
    RESULTS,
    answers_for,
    config,
    load_jsonl,
    score_both,
    update_summary,
    write_result,
)


def evaluate(pairs: list[dict], answers, cfg, group_key: str, score: str) -> tuple[dict, list[dict]]:
    rows = []
    groups: dict[str, list[bool]] = collections.defaultdict(list)
    for p in pairs:
        w, i = score_both(answers[p["weak"]], cfg)[score], score_both(answers[p["improved"]], cfg)[score]
        win = i > w
        groups[p[group_key]].append(win)
        rows.append(
            {
                "id": p["id"],
                "group": p[group_key],
                "weak_score": w,
                "improved_score": i,
                "delta": i - w,
                "weak_first_try": round(answers[p["weak"]].nouls["first_try_success"], 3),
                "improved_first_try": round(answers[p["improved"]].nouls["first_try_success"], 3),
                "correct": win,
            }
        )
    n = len(rows)
    result = {
        "n": n,
        "accuracy": sum(r["correct"] for r in rows) / n,
        "ties": sum(r["delta"] == 0 for r in rows),
        "mean_delta": sum(r["delta"] for r in rows) / n,
        "mean_weak_score": sum(r["weak_score"] for r in rows) / n,
        "mean_improved_score": sum(r["improved_score"] for r in rows) / n,
        "first_try_alone_accuracy": sum(r["improved_first_try"] > r["weak_first_try"] for r in rows) / n,
        f"by_{group_key}": {g: {"n": len(v), "accuracy": sum(v) / len(v)} for g, v in sorted(groups.items())},
        "failures": [r for r in rows if not r["correct"]],
    }
    return result, rows


async def main() -> None:
    cfg = config()
    easy = load_jsonl("prompt_pairs.jsonl")
    hard = load_jsonl("prompt_pairs_hard.jsonl")
    prompts = [p[k] for p in easy + hard for k in ("weak", "improved")]

    matrix: dict[str, dict[str, dict[str, float]]] = {}
    detail = {}
    all_rows = []
    latencies: list[int] = []
    for backend in ("jev", "heuristic"):
        answers, lat = await answers_for(backend, prompts, "pairwise")
        latencies += lat
        matrix[backend] = {}
        for score in ("lint", "pqs"):
            main_r, main_rows = evaluate(easy, answers, cfg, "task_type", score)
            hard_r, hard_rows = evaluate(hard, answers, cfg, "adds", score)
            matrix[backend][score] = {
                "main": main_r["accuracy"],
                "hard": hard_r["accuracy"],
                "main_mean_delta": main_r["mean_delta"],
                "hard_mean_delta": hard_r["mean_delta"],
            }
            detail[f"{backend}_{score}"] = {"main": main_r, "hard": hard_r}
            all_rows += [{"backend": backend, "score": score, "set": "main", **r} for r in main_rows]
            all_rows += [{"backend": backend, "score": score, "set": "hard", **r} for r in hard_rows]

    headline = detail["jev_lint"]
    headline["main"]["target"] = 0.85
    write_result("pairwise", {**headline, "matrix": matrix, "detail": detail})
    with (RESULTS / "pairwise_rows.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(all_rows[0]))
        wr.writeheader()
        wr.writerows(all_rows)
    update_summary(
        "pairwise",
        {
            "accuracy": headline["main"]["accuracy"],
            "n": headline["main"]["n"],
            "ties": headline["main"]["ties"],
            "hard_accuracy": headline["hard"]["accuracy"],
            "hard_n": headline["hard"]["n"],
            "matrix": matrix,
        },
        latencies=latencies,
    )

    print(f"\n{'':22s}{'main (200)':>12s}{'hard (40)':>12s}{'Δ main':>9s}{'Δ hard':>9s}")
    for backend in ("jev", "heuristic"):
        for score in ("lint", "pqs"):
            m = matrix[backend][score]
            print(
                f"{backend + ' · ' + score + '_score':22s}{m['main']:>12.1%}{m['hard']:>12.1%}"
                f"{m['main_mean_delta']:>+9.1f}{m['hard_mean_delta']:>+9.1f}"
            )
    fails = headline["hard"]["failures"] + headline["main"]["failures"]
    for f in fails[:8]:
        print(f"  ✗ jev·lint {f['id']}: {f['weak_score']} → {f['improved_score']}")


if __name__ == "__main__":
    asyncio.run(main())
