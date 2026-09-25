"""Eval 1 (PromptLint.md §12): does the Lint Score rank the improved prompt above the weak one?

    python eval/run_pairwise.py            # uses cached Jev answers where available

Two sets, reported separately:
  prompt_pairs.jsonl       200 pairs, vague vs well-specified (target ≥ 85%)
  prompt_pairs_hard.jsonl   40 near-miss pairs: a decent prompt vs the same prompt plus ONE missing piece
Ties count as failures.
"""

import asyncio
import collections
import csv

from common import RESULTS, config, judge_all, lint, load_jsonl, update_summary, write_result


def evaluate(pairs: list[dict], answers, cfg, group_key: str) -> tuple[dict, list[dict]]:
    rows = []
    groups: dict[str, list[bool]] = collections.defaultdict(list)
    verdicts = {"weak": collections.Counter(), "improved": collections.Counter()}
    for p in pairs:
        w, i = lint(answers[p["weak"]], cfg), lint(answers[p["improved"]], cfg)
        win = i.lint_score > w.lint_score
        groups[p[group_key]].append(win)
        verdicts["weak"][w.verdict] += 1
        verdicts["improved"][i.verdict] += 1
        rows.append(
            {
                "id": p["id"],
                "group": p[group_key],
                "weak_score": w.lint_score,
                "improved_score": i.lint_score,
                "delta": i.lint_score - w.lint_score,
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
        "verdicts": {k: dict(v) for k, v in verdicts.items()},
        "failures": [r for r in rows if not r["correct"]],
    }
    return result, rows


def report(title: str, r: dict, group_key: str) -> None:
    print(f"\n{title}: {r['accuracy']:.1%} of {r['n']} pairs (ties {r['ties']})")
    print(
        f"  mean score: weak {r['mean_weak_score']:.1f} → improved {r['mean_improved_score']:.1f} (Δ {r['mean_delta']:+.1f})"
    )
    print(f"  first-try probability alone would rank {r['first_try_alone_accuracy']:.1%} correctly")
    for g, v in r[f"by_{group_key}"].items():
        print(f"    {g:15s} {v['accuracy']:.0%} (n={v['n']})")
    for f in r["failures"][:8]:
        print(f"    ✗ {f['id']}: {f['weak_score']} → {f['improved_score']}")


async def main() -> None:
    cfg = config()
    easy = load_jsonl("prompt_pairs.jsonl")
    hard = load_jsonl("prompt_pairs_hard.jsonl")
    prompts = [p[k] for p in easy + hard for k in ("weak", "improved")]
    answers, latencies = await judge_all(prompts, label="pairwise")

    main_r, main_rows = evaluate(easy, answers, cfg, "task_type")
    hard_r, hard_rows = evaluate(hard, answers, cfg, "adds")
    main_r["target"] = 0.85
    write_result("pairwise", {"main": main_r, "hard": hard_r})
    with (RESULTS / "pairwise_rows.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["set", *main_rows[0]])
        wr.writeheader()
        wr.writerows([{"set": "main", **r} for r in main_rows] + [{"set": "hard", **r} for r in hard_rows])
    update_summary(
        "pairwise",
        {
            "accuracy": main_r["accuracy"],
            "n": main_r["n"],
            "ties": main_r["ties"],
            "hard_accuracy": hard_r["accuracy"],
            "hard_n": hard_r["n"],
        },
        latencies=latencies,
    )
    report("Main set (vague vs specific)", main_r, "task_type")
    report("Hard set (one missing piece)", hard_r, "adds")


if __name__ == "__main__":
    asyncio.run(main())
