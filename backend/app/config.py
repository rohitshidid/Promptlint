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
    id: str = ""
    above: float | None = None
    below: float | None = None
    weight: float | None = None


@dataclass(frozen=True)
class TipConfig:
    max_tips: int
    tips: tuple[TipRule, ...]


# Component → (source signal, how to turn it into P(missing)). "noul": 1 − p; "score": 1 − score/top.
PQS_COMPONENT_SOURCES: dict[str, str] = {
    "goal": "has_goal",
    "context": "context_given",
    "constraints": "has_constraints",
    "output_format": "has_output_format",
    "audience": "has_audience",
    "examples": "has_examples",
    "success_criteria": "has_success_criteria",
}


@dataclass(frozen=True)
class PqsConfig:
    weights: dict[str, float]  # clarity, specificity, completeness, no_reiteration
    components: dict[str, float]
    task_type_overrides: dict[str, dict[str, float]]
    missing_threshold: float

    def component_weights(self, task_type: str) -> dict[str, float]:
        over = self.task_type_overrides.get(task_type, {})
        return {c: w * over.get(c, 1.0) for c, w in self.components.items()}


@dataclass(frozen=True)
class Plan:
    name: str
    rpm: int
    daily: int | None
    monthly: int | None
    batch_max: int


@dataclass(frozen=True)
class PlanConfig:
    default_plan: str
    max_keys_per_user: int
    plans: dict[str, Plan]

    def get(self, name: str) -> Plan:
        return self.plans.get(name) or self.plans[self.default_plan]


@dataclass(frozen=True)
class RoutingConfig:
    default_strength: float = 0.9
    min_edge: float = 0.1
    balanced_price_band: float = 3.0
    task_strengths: dict[str, dict[str, float]] = field(default_factory=dict)

    def strength(self, task_type: str, model_id: str, provider: str) -> float:
        """How well a model fits a task type: the model's own entry, else its provider's, else the default."""
        row = self.task_strengths.get(task_type, {})
        if model_id in row:
            return row[model_id]
        return row.get(provider.lower(), self.default_strength)


@dataclass(frozen=True)
class AppConfig:
    questions: QuestionConfig
    weights: WeightConfig
    prices: PriceConfig
    tips: TipConfig
    pqs: PqsConfig
    plans: PlanConfig
    score_tops: dict[str, int] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)


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


def load_pqs(path: Path) -> PqsConfig:
    raw = _load(path)
    return PqsConfig(
        weights={k: float(v) for k, v in raw["weights"].items()},
        components={k: float(v) for k, v in raw["components"].items()},
        task_type_overrides={
            t: {c: float(m) for c, m in over.items()}
            for t, over in (raw.get("task_type_overrides") or {}).items()
        },
        missing_threshold=float(raw.get("missing_threshold", 0.5)),
    )


def load_plans(path: Path) -> PlanConfig:
    raw = _load(path)
    return PlanConfig(
        default_plan=raw["default_plan"],
        max_keys_per_user=int(raw.get("max_keys_per_user", 5)),
        plans={name: Plan(name=name, **p) for name, p in raw["plans"].items()},
    )


def load_routing(path: Path) -> RoutingConfig:
    if not path.exists():
        return RoutingConfig()
    raw = _load(path)
    return RoutingConfig(
        default_strength=float(raw.get("default_strength", 0.9)),
        min_edge=float(raw.get("min_edge", 0.1)),
        balanced_price_band=float(raw.get("balanced_price_band", 3.0)),
        task_strengths={
            t: {str(k): float(v) for k, v in (row or {}).items()}
            for t, row in (raw.get("task_strengths") or {}).items()
        },
    )


def load_config(config_dir: Path) -> AppConfig:
    questions = load_questions(config_dir / "questions.yaml")
    cfg = AppConfig(
        questions=questions,
        weights=load_weights(config_dir / "weights.yaml"),
        prices=load_prices(config_dir / "prices.yaml"),
        tips=load_tips(config_dir / "tips.yaml"),
        pqs=load_pqs(config_dir / "pqs_scoring.yaml"),
        plans=load_plans(config_dir / "plans.yaml"),
        score_tops={k: q.top for k, q in questions.scores.items()},
        routing=load_routing(config_dir / "routing.yaml"),
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
    if set(cfg.pqs.weights) != {"clarity", "specificity", "completeness", "no_reiteration"}:
        raise ValueError(
            "pqs_scoring.yaml weights must be clarity, specificity, completeness, no_reiteration"
        )
    for comp in cfg.pqs.components:
        source = PQS_COMPONENT_SOURCES.get(comp)
        if source is None or source not in known:
            raise ValueError(f"pqs_scoring.yaml component {comp!r} has no matching Jev question")
    task_types = set(cfg.questions.choices.get("task_type", ("", {}))[1])
    for t in cfg.pqs.task_type_overrides:
        if t not in task_types:
            raise ValueError(f"pqs_scoring.yaml override for unknown task type {t!r}")
    if cfg.plans.default_plan not in cfg.plans.plans:
        raise ValueError("plans.yaml default_plan is not a defined plan")
    if cfg.score_tops.get("complexity") != len(cfg.weights.tier_hint) - 1:
        raise ValueError("weights.yaml tier_hint needs one entry per complexity level")
    r = cfg.routing
    if r.balanced_price_band < 1:
        raise ValueError("routing.yaml balanced_price_band must be at least 1")
    providers = {m.provider.lower() for m in cfg.prices.models}
    model_ids = {m.id for m in cfg.prices.models}
    for t, row in r.task_strengths.items():
        if t not in task_types:
            raise ValueError(f"routing.yaml has strengths for unknown task type {t!r}")
        for k, v in row.items():
            if k not in providers and k not in model_ids:
                raise ValueError(f"routing.yaml {t}: {k!r} is not a provider or model ID in prices.yaml")
            if not 0 <= v <= 1:
                raise ValueError(f"routing.yaml {t}.{k} must be between 0 and 1")
