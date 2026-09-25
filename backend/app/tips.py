"""Rule-based fix-it tips (PromptLint.md §9): failed checks -> tips, ranked by weight × shortfall."""

from dataclasses import dataclass

from app.config import TipConfig, WeightConfig


@dataclass(frozen=True)
class Tip:
    signal: str
    text: str
    impact: float


def select_tips(raw: dict[str, float], tips: TipConfig, weights: WeightConfig) -> list[Tip]:
    fired: list[Tip] = []
    for rule in tips.tips:
        value = raw.get(rule.signal)
        if value is None:
            continue
        if rule.below is not None and value < rule.below:
            shortfall = 1.0 - value  # a "good when high" signal falling short
        elif rule.above is not None and value > rule.above:
            shortfall = value  # a "bad when high" signal
        else:
            continue
        weight = rule.weight if rule.weight is not None else weights.weights[rule.signal].weight
        fired.append(Tip(rule.signal, rule.text, round(weight * shortfall, 4)))
    fired.sort(key=lambda t: t.impact, reverse=True)
    return fired[: tips.max_tips]
