"""Routing savings: how much cheaper is routing each prompt than always using one model?

    python eval/run_routing.py

Routes all 480 prompts from the pairwise sets (weak and improved versions of 240 requests) with each
strategy over the 7 priced models, using the cached Jev answers, and compares the expected cost with
"always use model X". Writes eval/results/routing.json and frontend/assets/routing-summary.json, which
feeds the landing page's savings section and calculator.

Caveats, shown on the site: costs are expected costs (input tokens × price + the router's p50 output
estimate); real savings depend on your traffic mix, and on whether the cheaper model is good enough for
your quality bar. The mix here is our test set, not production traffic.
"""

import asyncio
import collections
import json
from datetime import UTC, datetime

from common import RESULTS, ROOT, config, judge_all, load_jsonl, write_result  # puts backend/ on sys.path

# isort: split
from app.analyze import Analyzer
from app.backends import BackendRouter, HeuristicJudge
from app.schemas import RoutingOptions
from app.tokens import TokenCounter

PUBLIC = ROOT / "frontend" / "assets" / "routing-summary.json"


class CachedJudge:
    """Replays cached Jev answers so routing needs no new API calls."""

    name = "jev"

    def __init__(self, answers):
        self.answers = answers

    async def judge(self, prompt, system=None):
        return self.answers[prompt]


async def main() -> None:
    cfg = config()
    pairs = load_jsonl("prompt_pairs.jsonl") + load_jsonl("prompt_pairs_hard.jsonl")
    weak = [p["weak"] for p in pairs]
    prompts = weak + [p["improved"] for p in pairs]
    answers, _ = await judge_all(prompts, label="routing")

    judge = CachedJudge(answers)
    analyzer = Analyzer(
        router=BackendRouter(jev=judge, heuristic=HeuristicJudge(cfg.questions)),
        counter=TokenCounter(),
        config=cfg,
        cache_size=0,
    )
    catalog = analyzer.catalog_candidates()
    weak_set = set(weak)

    totals = collections.defaultdict(float)  # strategy → total expected cost
    baseline_totals = collections.defaultdict(float)  # model → total cost if always used
    mix = {s: collections.Counter() for s in ("cheapest", "balanced", "quality")}
    picks = {s: collections.Counter() for s in ("cheapest", "balanced", "quality")}
    clarify = clarify_weak = 0
    for prompt in prompts:
        a = await analyzer.analyze(prompt)
        for c in catalog:
            baseline_totals[c.id] += c.cost(a.input_o200k, a.output_p50)
        for strategy in mix:
            d = analyzer.decide(a, RoutingOptions(strategy=strategy), catalog)
            totals[strategy] += d.chosen.cost_p50
            mix[strategy][d.chosen.candidate.tier] += 1
            picks[strategy][d.chosen.candidate.id] += 1
            if strategy == "balanced" and d.action == "clarify_first":
                clarify += 1
                clarify_weak += prompt in weak_set

    n = len(prompts)
    names = {c.id: c.name for c in catalog}
    avg = {s: totals[s] / n for s in totals}
    base_avg = {m: baseline_totals[m] / n for m in baseline_totals}
    savings = {
        s: {m: round(100 * (1 - avg[s] / base_avg[m]), 1) if base_avg[m] else 0.0 for m in base_avg}
        for s in avg
    }
    frontier = [c.id for c in catalog if c.tier == "frontier"]
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_prompts": n,
        "prices_last_updated": cfg.prices.last_updated,
        "strategies": {
            s: {
                "avg_cost_usd": avg[s],
                "tier_mix": {t: mix[s][t] / n for t in ("small", "mid", "frontier")},
                "top_picks": [
                    {"model": m, "name": names[m], "share": k / n} for m, k in picks[s].most_common(4)
                ],
            }
            for s in ("cheapest", "balanced", "quality")
        },
        "baselines": {m: {"name": names[m], "avg_cost_usd": base_avg[m]} for m in base_avg},
        "savings_percent": savings,
        "headline": {
            "balanced_vs_frontier_avg": round(
                sum(savings["balanced"][m] for m in frontier) / len(frontier), 1
            ),
            "balanced_vs_priciest": max(savings["balanced"].values()),
            "priciest_model": max(base_avg, key=base_avg.get),
            "clarify_first_share": clarify / n,
            "clarify_first_weak_share": clarify_weak / len(weak),
        },
    }
    write_result("routing", summary)
    PUBLIC.write_text(json.dumps(summary, indent=2) + "\n")

    print(f"\nRouting {n} prompts over {len(catalog)} models (expected cost per request)")
    for s in ("cheapest", "balanced", "quality"):
        m = summary["strategies"][s]
        tiers = " ".join(f"{t} {m['tier_mix'][t]:.0%}" for t in ("small", "mid", "frontier"))
        print(f"  {s:9s} ${avg[s]:.6f}   tiers: {tiers}")
    print("  always one model:")
    for mid, v in sorted(base_avg.items(), key=lambda kv: -kv[1]):
        print(f"    {names[mid]:18s} ${v:.6f}   balanced saves {savings['balanced'][mid]:+.1f}%")
    h = summary["headline"]
    print(f"  balanced vs frontier models (avg): {h['balanced_vs_frontier_avg']}% cheaper")
    print(
        f"  clarify_first: {h['clarify_first_share']:.1%} of all prompts, {h['clarify_first_weak_share']:.1%} of weak ones"
    )
    print(f"wrote {RESULTS / 'routing.json'} and {PUBLIC.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
