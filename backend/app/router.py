"""Model router: picks which LLM should answer a prompt, from Jev's judgment of the prompt.

Pure functions, no I/O. The API adds the result to every /v1/score response as `routing`, and
/v1/route can go on to call the chosen model (app/providers.py).

How a pick is made
  1. Required tier. Jev's complexity answer is a distribution over low / medium / high. A strategy
     sets how sure we want to be that the model is strong enough:
         cheapest  → the tier where P(complexity ≤ tier) ≥ 0.50
         balanced  → … ≥ 0.75   (default)
         quality   → … ≥ 0.90, and then prefer the strongest tier available
  2. Candidates. The caller's models (catalog IDs or custom entries with prices and a tier), or our
     whole price table. A candidate is "capable" when its tier ≥ the required tier.
  3. Rank. cheapest/balanced: capable models, lowest adequate tier first, then lowest expected cost.
     quality: highest tier first, then the best fit for the task type, then lowest cost. An optional
     per-request budget removes models whose p90 cost exceeds it.
     Task fit (config/routing.yaml) is a 0–1 rating of each model for each task type. balanced starts
     from the cheapest capable model and switches to a better-fitting capable one when it is at least
     `min_edge` better and costs at most `price_band` × as much. The band is measured from at least the
     cheapest built-in model of that tier (`band_floor`), so a free or near-free custom endpoint can't
     shut every better fit out. cheapest ignores fit.
  4. Action. If the prompt is likely to fail anyway (verdict likely_to_fail and low first-try odds),
     the action is `clarify_first`: ask the user for the missing pieces before paying for a call.
  5. Savings. Expected cost of the pick vs a baseline model (the caller's, or their priciest).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

TIERS = ("small", "mid", "frontier")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}
STRATEGIES = ("cheapest", "balanced", "quality")
CONFIDENCE = {"cheapest": 0.50, "balanced": 0.75, "quality": 0.90}
TIER_WORDS = {"small": "small", "mid": "mid-tier", "frontier": "frontier"}
COMPLEXITY_WORDS = ("simple", "medium-difficulty", "hard")

# Which provider adapter can call a catalog model, from prices.yaml's provider name.
ADAPTER_FOR_PROVIDER = {"anthropic": "anthropic", "openai": "openai", "google": "gemini"}


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    provider: str  # display name, e.g. "Anthropic"
    adapter: str  # anthropic | openai | gemini | openai_compatible
    tier: str
    input: float  # USD per 1M input tokens
    output: float  # USD per 1M output tokens
    source: str = "catalog"  # catalog | custom | connected
    base_url: str | None = None  # openai_compatible only
    api_model: str | None = None  # the model name the provider expects (defaults to id)
    inline_key: str | None = field(
        default=None, repr=False, compare=False
    )  # per-request key for a custom model

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output) / 1_000_000


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    capable: bool
    cost_p50: float
    cost_p90: float
    within_budget: bool
    fit: float = 1.0  # how well the model suits this task type (routing.yaml)


@dataclass(frozen=True)
class RouteDecision:
    strategy: str
    action: str  # send | clarify_first
    required_tier: str
    complexity: str
    chosen: Ranked | None
    fallback: Ranked | None
    ranked: list[Ranked]
    baseline: Ranked | None
    savings_usd: float
    savings_percent: float
    reason: str
    clarify_reason: str | None
    warnings: list[str]
    task_type: str = "other"


def required_tier(complexity_probs: Sequence[float], strategy: str) -> str:
    """Lowest tier we're `CONFIDENCE[strategy]` sure is strong enough."""
    total = sum(complexity_probs) or 1.0
    acc = 0.0
    for i, p in enumerate(complexity_probs):
        acc += p / total
        if acc >= CONFIDENCE[strategy] - 1e-9:
            return TIERS[min(i, len(TIERS) - 1)]
    return TIERS[-1]


def _rank_key(r: Ranked, strategy: str, need: int) -> tuple:
    tier = TIER_RANK[r.candidate.tier]
    if strategy == "quality":
        # Strongest first; within a tier, the best fit for the task, then cheapest.
        return (not r.within_budget, not r.capable, -tier, -r.fit, r.cost_p50, r.candidate.id)
    # cheapest / balanced: capable first, the lowest adequate tier, then cheapest.
    return (
        not r.within_budget,
        not r.capable,
        tier - need if r.capable else -tier,
        r.cost_p50,
        r.candidate.id,
    )


def route(
    *,
    candidates: Sequence[Candidate],
    complexity_probs: Sequence[float],
    input_tokens: int,
    output_p50: int,
    output_p90: int,
    verdict: str,
    first_try: float,
    missing: Sequence[str],
    task_type: str,
    strategy: str = "balanced",
    baseline_id: str | None = None,
    max_cost_usd: float | None = None,
    fits: dict[str, float] | None = None,
    min_edge: float = 0.1,
    price_band: float = 3.0,
    band_floor: dict[str, float] | None = None,
) -> RouteDecision:
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
    warnings: list[str] = []
    need_tier = required_tier(complexity_probs, strategy)
    need = TIER_RANK[need_tier]
    level = max(range(len(complexity_probs)), key=lambda i: complexity_probs[i])
    complexity = COMPLEXITY_WORDS[min(level, 2)]

    ranked = [
        Ranked(
            candidate=c,
            capable=TIER_RANK[c.tier] >= need,
            cost_p50=c.cost(input_tokens, output_p50),
            cost_p90=c.cost(input_tokens, output_p90),
            within_budget=max_cost_usd is None or c.cost(input_tokens, output_p90) <= max_cost_usd,
            fit=(fits or {}).get(c.id, 1.0),
        )
        for c in candidates
    ]
    ranked.sort(key=lambda r: _rank_key(r, strategy, need))

    # balanced: pay a bit more for a clearly better fit for this kind of task.
    cheapest_capable = None
    band_base = 0.0
    if strategy == "balanced" and ranked and ranked[0].capable and ranked[0].within_budget:
        base = ranked[0]
        band_base = max(base.cost_p50, (band_floor or {}).get(base.candidate.tier, 0.0))
        better = [
            r
            for r in ranked[1:]
            if r.capable
            and r.within_budget
            and r.fit >= base.fit + min_edge - 1e-9
            and r.cost_p50 <= band_base * price_band + 1e-12
        ]
        if better:
            pick = max(better, key=lambda r: (r.fit, -r.cost_p50))
            ranked.remove(pick)
            ranked.insert(0, pick)
            cheapest_capable = base

    if ranked and not any(r.capable for r in ranked):
        warnings.append(
            f"None of the available models is rated {TIER_WORDS[need_tier]} or above; using the strongest one."
        )
        ranked.sort(key=lambda r: (not r.within_budget, -TIER_RANK[r.candidate.tier], r.cost_p50))
    if max_cost_usd is not None and ranked and not ranked[0].within_budget:
        warnings.append(f"No model fits the ${max_cost_usd:g} budget; showing the best option anyway.")

    chosen = ranked[0] if ranked else None
    fallback = next((r for r in ranked[1:] if r.capable), ranked[1] if len(ranked) > 1 else None)

    if baseline_id:
        baseline = next((r for r in ranked if r.candidate.id == baseline_id), None)
        if baseline is None:
            warnings.append(
                f"Baseline {baseline_id!r} isn't among the candidates; comparing with the priciest instead."
            )
    else:
        baseline = None
    if baseline is None and ranked:
        baseline = max(ranked, key=lambda r: (r.cost_p50, TIER_RANK[r.candidate.tier]))

    savings = max(0.0, baseline.cost_p50 - chosen.cost_p50) if baseline and chosen else 0.0
    percent = (savings / baseline.cost_p50 * 100) if baseline and baseline.cost_p50 > 0 else 0.0

    clarify = verdict == "likely_to_fail" and first_try < 0.4
    clarify_reason = None
    if clarify:
        pieces = ", ".join(m.replace("_", " ") for m in list(missing)[:3]) or "more detail"
        clarify_reason = (
            f"This prompt will probably need a second try whichever model answers. Ask the user for {pieces} "
            "first, so you don't pay for an answer that misses."
        )

    reason = ""
    if chosen:
        c = chosen.candidate
        task = task_type.replace("_", " ")
        if strategy == "quality":
            same_tier = [r for r in ranked if r.candidate.tier == c.tier and r is not chosen]
            best_fit = any(r.fit < chosen.fit for r in same_tier)
            reason = (
                f"Quality first: {c.name} is in the strongest tier available ({TIER_WORDS[c.tier]}) and is "
                + (f"the best fit for {task} prompts in it" if best_fit else "the lowest-cost model in it")
                + (" within your budget" if max_cost_usd else "")
            )
        elif cheapest_capable is not None:
            ratio = chosen.cost_p50 / band_base if band_base else 1.0
            cost_words = (
                f"costs {ratio:.1f}× as much"
                if band_base <= cheapest_capable.cost_p50
                else f"costs {ratio:.1f}× the cheapest built-in {TIER_WORDS[cheapest_capable.candidate.tier]} model"
            )
            reason = (
                f"A {complexity} {task} prompt needs a {TIER_WORDS[need_tier]} model or better. "
                f"{c.name} is a better fit for {task} than {cheapest_capable.candidate.name} "
                f"(the cheapest that qualifies) and {cost_words}"
            )
        elif chosen.capable:
            reason = (
                f"A {complexity} {task} prompt needs a {TIER_WORDS[need_tier]} model or better; "
                f"{c.name} is the {'cheapest' if strategy == 'cheapest' else 'best-value'} one that qualifies"
            )
        else:
            reason = f"{c.name} is the strongest model available"
        if baseline and baseline.candidate.id != c.id and savings > 0:
            joiner = ". It's still" if cheapest_capable is not None else ","
            amount = "over 99%" if percent >= 99.5 else f"about {int(percent + 0.5)}%"
            reason += f"{joiner} {amount} cheaper than {baseline.candidate.name}"
        reason += "."

    return RouteDecision(
        strategy=strategy,
        task_type=task_type,
        action="clarify_first" if clarify else "send",
        required_tier=need_tier,
        complexity=complexity,
        chosen=chosen,
        fallback=fallback,
        ranked=ranked,
        baseline=baseline,
        savings_usd=savings,
        savings_percent=percent,
        reason=reason,
        clarify_reason=clarify_reason,
        warnings=warnings,
    )
