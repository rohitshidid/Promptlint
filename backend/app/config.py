"""Loads the YAML config files (questions, weights, prices, tips) into typed objects."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class ScoreQuestion:
    instructions: str
    criteria: tuple[str, ...]

    @property
    def top(self) -> int:
        return len(self.criteria) - 1


@dataclass(frozen=True)
class QuestionConfig:
    nouls: dict[str, str]
    scores: dict[str, ScoreQuestion]
    choices: dict[str, tuple[str, dict[str, str]]]
    noul_criteria: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # optional {true, false} descriptions


@dataclass(frozen=True)
class SignalWeight:
    weight: float
    invert: bool = False


@dataclass(frozen=True)
class Cap:
    signal: str
    cap: int
    above: float | None = None
    below: float | None = None


@dataclass(frozen=True)
class WeightConfig:
    weights: dict[str, SignalWeight]
    caps: tuple[Cap, ...]
    ready_to_send: int
    needs_work: int
    low_confidence_below: float
    specificity_labels: tuple[tuple[float, str], ...]
    tier_hint: tuple[tuple[float, str], ...]


@dataclass(frozen=True)
class ModelPrice:
    id: str
    name: str
    provider: str
    input: float  # USD per million input tokens
    output: float  # USD per million output tokens
    tokenizer: str
    tier: str
    exact: bool = True
    note: str | None = None


@dataclass(frozen=True)
class PriceConfig:
    last_updated: str
    models: tuple[ModelPrice, ...]
    default_models: tuple[str, ...]
    output_ranges: tuple[tuple[int, int], ...]

    def get(self, model_id: str) -> ModelPrice | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True)
class TipRule:
    signal: str
    text: str
    above: float | None = None
    below: float | None = None
    weight: float | None = None


@dataclass(frozen=True)
class TipConfig:
    max_tips: int
    tips: tuple[TipRule, ...]


@dataclass(frozen=True)
class AppConfig:
    questions: QuestionConfig
    weights: WeightConfig
    prices: PriceConfig
    tips: TipConfig
    score_tops: dict[str, int] = field(default_factory=dict)


def load_questions(path: Path) -> QuestionConfig:
    raw = _load(path)
    return QuestionConfig(
        nouls={k: v["instructions"] for k, v in (raw.get("nouls") or {}).items()},
        scores={
            k: ScoreQuestion(v["instructions"], tuple(v["criteria"]))
            for k, v in (raw.get("scores") or {}).items()
        },
        choices={k: (v["instructions"], dict(v["criteria"])) for k, v in (raw.get("choices") or {}).items()},
        noul_criteria={
            k: {str(ck).lower(): cv for ck, cv in v["criteria"].items()}
            for k, v in (raw.get("nouls") or {}).items()
            if v.get("criteria")
        },
    )


def load_weights(path: Path) -> WeightConfig:
    raw = _load(path)
    return WeightConfig(
        weights={k: SignalWeight(**v) for k, v in raw["weights"].items()},
        caps=tuple(Cap(**c) for c in raw.get("caps", [])),
        ready_to_send=int(raw["verdicts"]["ready_to_send"]),
        needs_work=int(raw["verdicts"]["needs_work"]),
        low_confidence_below=float(raw.get("low_confidence_below", 0.5)),
        specificity_labels=tuple((float(x["below"]), x["label"]) for x in raw["specificity_labels"]),
        tier_hint=tuple((float(x["below"]), x["tier"]) for x in raw["tier_hint"]),
    )


def load_prices(path: Path) -> PriceConfig:
    raw = _load(path)
    return PriceConfig(
        last_updated=str(raw["last_updated"]),
        models=tuple(ModelPrice(**m) for m in raw["models"]),
        default_models=tuple(raw.get("default_models", [])),
        output_ranges=tuple((int(lo), int(hi)) for lo, hi in raw["output_ranges"]),
    )


def load_tips(path: Path) -> TipConfig:
    raw = _load(path)
    return TipConfig(max_tips=int(raw.get("max_tips", 5)), tips=tuple(TipRule(**t) for t in raw["tips"]))


def load_config(config_dir: Path) -> AppConfig:
    questions = load_questions(config_dir / "questions.yaml")
    cfg = AppConfig(
        questions=questions,
        weights=load_weights(config_dir / "weights.yaml"),
        prices=load_prices(config_dir / "prices.yaml"),
        tips=load_tips(config_dir / "tips.yaml"),
        score_tops={k: q.top for k, q in questions.scores.items()},
    )
    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    """Fail at startup, not mid-request, when the YAML files disagree with each other."""
    known = set(cfg.questions.nouls) | set(cfg.questions.scores)
    for name in cfg.weights.weights:
        if name not in known:
            raise ValueError(f"weights.yaml references unknown signal {name!r}")
    for cap in cfg.weights.caps:
        if cap.signal not in cfg.questions.nouls:
            raise ValueError(f"caps must reference a noul, got {cap.signal!r}")
    for tip in cfg.tips.tips:
        if tip.signal not in known:
            raise ValueError(f"tips.yaml references unknown signal {tip.signal!r}")
        if tip.weight is None and tip.signal not in cfg.weights.weights:
            raise ValueError(f"tip for {tip.signal!r} needs its own weight (signal is not scored)")
    for model_id in cfg.prices.default_models:
        if cfg.prices.get(model_id) is None:
            raise ValueError(f"default model {model_id!r} is not in prices.yaml")
    if "expected_length" in cfg.score_tops and cfg.score_tops["expected_length"] + 1 != len(
        cfg.prices.output_ranges
    ):
        raise ValueError("prices.yaml output_ranges must have one range per expected_length level")
