"""Composite Lint Score (PromptLint.md §7). Pure functions; no I/O.

Two views of every answer:
  raw     — noul probability, or score / top_level. Direction as Jev reports it.
  signal  — raw, inverted where `invert: true`, so 1 is always good. Only weighted signals.
"""

from dataclasses import dataclass

from app.config import WeightConfig
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
