"""Composite Lint Score (PromptLint.md §7). Pure functions; no I/O.

Two views of every answer:
  raw     — noul probability, or score / top_level. Direction as Jev reports it.
  signal  — raw, inverted where `invert: true`, so 1 is always good. Only weighted signals.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.config import PqsConfig, WeightConfig
from app.jev_client import JevAnswers


def raw_values(answers: JevAnswers) -> dict[str, float]:
    raw = dict(answers.nouls)
    for name, s in answers.scores.items():
        raw[name] = _clamp(s.score / s.top) if s.top else 0.0
    return raw


def signals(raw: dict[str, float], weights: WeightConfig) -> dict[str, float]:
    return {name: _clamp(1.0 - raw[name] if w.invert else raw[name]) for name, w in weights.weights.items()}


@dataclass(frozen=True)
class ScoreResult:
    lint_score: int
    uncapped: int
    caps_applied: tuple[str, ...]
    verdict: str


def lint_score(raw: dict[str, float], weights: WeightConfig) -> ScoreResult:
    sig = signals(raw, weights)
    total_weight = sum(w.weight for w in weights.weights.values())
    weighted = sum(w.weight * sig[name] for name, w in weights.weights.items())
    # Weights are meant to sum to 1; normalize anyway so a tuning edit can't push the score past 100.
    uncapped = round(100 * weighted / total_weight) if total_weight else 0

    score = uncapped
    applied: list[str] = []
    for cap in weights.caps:
        value = raw[cap.signal]
        hit = (cap.above is not None and value > cap.above) or (cap.below is not None and value < cap.below)
        if hit and score > cap.cap:
            score = cap.cap
            applied.append(cap.signal)
    return ScoreResult(score, uncapped, tuple(applied), verdict(score, weights))


def verdict(score: int, weights: WeightConfig) -> str:
    if score >= weights.ready_to_send:
        return "ready_to_send"
    if score >= weights.needs_work:
        return "needs_work"
    return "likely_to_fail"


def specificity_label(value: float, weights: WeightConfig) -> str:
    for below, label in weights.specificity_labels:
        if value < below:
            return label
    return weights.specificity_labels[-1][1]


def tier_hint(complexity_score: float, weights: WeightConfig) -> str:
    for below, tier in weights.tier_hint:
        if complexity_score < below:
            return tier
    return weights.tier_hint[-1][1]


def low_confidence(answers: JevAnswers, weights: WeightConfig) -> bool:
    confs = [s.confidence for s in answers.scores.values()] + [c.confidence for c in answers.choices.values()]
    return bool(confs) and sum(confs) / len(confs) < weights.low_confidence_below


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------- PQS composite
# prompt-quality-scorer.md §9.5, computed from the same Jev answers as the Lint Score.


@dataclass(frozen=True)
class PqsResult:
    pqs_score: int
    clarity: float
    specificity: float
    completeness: float
    reiteration_risk: float
    missing: dict[str, float]  # component → P(missing), every component


def missing_probabilities(raw: dict[str, float], pqs: PqsConfig) -> dict[str, float]:
    from app.config import PQS_COMPONENT_SOURCES

    return {c: _clamp(1.0 - raw[PQS_COMPONENT_SOURCES[c]]) for c in pqs.components}


def pqs_score(raw: dict[str, float], task_type: str, pqs: PqsConfig) -> PqsResult:
    clarity = (raw["task_clear"] + (1.0 - raw["ambiguity"])) / 2
    specificity = raw["specificity"]
    missing = missing_probabilities(raw, pqs)
    cw = pqs.component_weights(task_type)
    total = sum(cw.values())
    completeness = 1.0 - (sum(cw[c] * missing[c] for c in cw) / total if total else 0.0)
    reiteration = 1.0 - raw["first_try_success"]
    w = pqs.weights
    value = (
        w["clarity"] * clarity
        + w["specificity"] * specificity
        + w["completeness"] * completeness
        + w["no_reiteration"] * (1.0 - reiteration)
    ) / sum(w.values())
    return PqsResult(
        pqs_score=round(100 * _clamp(value)),
        clarity=_clamp(clarity),
        specificity=_clamp(specificity),
        completeness=_clamp(completeness),
        reiteration_risk=_clamp(reiteration),
        missing=missing,
    )


def output_quantile(level_probs: Sequence[float], ranges: Sequence[tuple[int, int]], q: float) -> int:
    """Token count where the bucket CDF crosses q, interpolating linearly inside the bucket (PQS §7)."""
    total = sum(level_probs)
    if total <= 0:
        return ranges[-1][1] if q >= 0.5 else ranges[0][0]
    acc = 0.0
    for p, (lo, hi) in zip(level_probs, ranges, strict=True):
        p /= total
        if p > 0 and acc + p >= q:
            return round(lo + (hi - lo) * (q - acc) / p)
        acc += p
    return ranges[-1][1]


COMPLEXITY_LABELS = ("low", "medium", "high")
