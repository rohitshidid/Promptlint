"""Prompt-injection check (PromptLint.md §14): how much does text like "rate this 100" inflate a score?

    python eval/run_injection.py
Each row is a weak prompt with and without an injected instruction aimed at the grader.
"""

import asyncio

from common import config, judge_all, lint, load_jsonl, update_summary, write_result


async def main() -> None:
    rows = load_jsonl("injection.jsonl")
    cfg = config()
    answers, latencies = await judge_all(
        [r["base"] for r in rows] + [r["injected"] for r in rows], label="injection"
    )

    out = []
    for r in rows:
        b, i = lint(answers[r["base"]], cfg), lint(answers[r["injected"]], cfg)
        out.append(
            {
                "id": r["id"],
                "base": b.lint_score,
                "injected": i.lint_score,
                "inflation": i.lint_score - b.lint_score,
                "injected_verdict": i.verdict,
            }
        )
    inflations = [o["inflation"] for o in out]
    promoted = sum(o["injected_verdict"] == "ready_to_send" for o in out)
    result = {
        "n": len(out),
        "mean_inflation": sum(inflations) / len(out),
        "max_inflation": max(inflations),
        "promoted_to_ready": promoted,
        "rows": out,
    }
    write_result("injection", result)
    update_summary(
        "injection",
        {
            "mean_inflation": result["mean_inflation"],
            "max_inflation": result["max_inflation"],
            "promoted_to_ready": promoted,
            "n": len(out),
        },
        latencies=latencies,
    )
    print(
        f"\nInjection: mean inflation {result['mean_inflation']:+.1f} pts, max {result['max_inflation']:+d}, "
        f"{promoted}/{len(out)} pushed to 'Ready to send'"
    )
    for o in sorted(out, key=lambda o: -o["inflation"])[:5]:
        print(f"  {o['id']}: {o['base']} → {o['injected']} ({o['inflation']:+d})")


if __name__ == "__main__":
    asyncio.run(main())
