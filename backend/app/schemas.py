"""Request and response models for /api/analyze (PromptLint.md §10, plus a few display fields)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Verdict = Literal["ready_to_send", "needs_work", "likely_to_fail"]
Tier = Literal["small", "mid", "frontier"]


Backend = Literal["auto", "jev", "heuristic"]
Strategy = Literal["cheapest", "balanced", "quality"]
Adapter = Literal["anthropic", "openai", "gemini", "openai_compatible"]


class CustomModel(BaseModel):
    """A model the caller has that isn't in our price table (or has its own prices)."""

    id: str = Field(
        ..., min_length=1, max_length=100, description="Your name for it; echoed back when chosen."
    )
    name: str | None = Field(None, max_length=100)
    provider: Adapter = "openai_compatible"
    tier: Literal["small", "mid", "frontier"]
    input_price: float = Field(..., ge=0, description="USD per 1M input tokens")
    output_price: float = Field(..., ge=0, description="USD per 1M output tokens")
    base_url: str | None = Field(
        None, max_length=500, description="openai_compatible: e.g. https://api.groq.com/openai/v1"
    )
    model: str | None = Field(
        None, max_length=200, description="Model name the provider expects; defaults to id"
    )
    api_key: str | None = Field(
        None, max_length=500, description="Per-request key for this model (never stored)"
    )


class RoutingOptions(BaseModel):
    strategy: Strategy = "balanced"
    candidates: list[str | CustomModel] | None = Field(
        None,
        description="Models to choose from: price-table IDs and/or custom models. Default: the whole price table.",
    )
    include_connected: bool = Field(True, description="Also consider custom endpoints saved on your account.")
    baseline_model: str | None = Field(
        None, description="Model to compare savings against. Default: the priciest candidate."
    )
    max_cost_usd: float | None = Field(
        None, gt=0, description="Skip models whose p90 cost for this prompt exceeds this."
    )

    @field_validator("candidates")
    @classmethod
    def _limit(cls, v):
        if v is not None and not 1 <= len(v) <= 30:
            raise ValueError("candidates must list 1 to 30 models")
        return v


class RoutedModel(BaseModel):
    id: str
    name: str
    provider: str
    tier: str
    source: str
    capable: bool
    est_cost_usd_p50: float
    est_cost_usd_p90: float
    task_fit: float | None = Field(None, description="0–1: how well the model suits this task type")
    connected: bool | None = Field(
        None, description="Whether you have a key for this model (only set once you've connected any)"
    )


class NotConnected(BaseModel):
    """The best fit is a model you haven't connected; `instead` is the best of the ones you have."""

    model: RoutedModel
    instead: RoutedModel | None
    note: str


class Routing(BaseModel):
    strategy: Strategy
    task_type: str | None = None
    action: Literal["send", "clarify_first"]
    required_tier: str
    complexity: str
    recommended: RoutedModel | None
    fallback: RoutedModel | None
    reason: str
    clarify_reason: str | None = None
    baseline: RoutedModel | None
    savings_usd: float
    savings_percent: float
    alternatives: list[RoutedModel]
    not_connected: NotConnected | None = None
    warnings: list[str] = []


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to lint (1–20,000 characters).")
    models: list[str] | None = Field(None, description="Price-table model IDs; defaults apply when empty.")
    backend: Backend = Field("auto", description="auto = Jev with heuristic fallback")
    strategy: Strategy = "balanced"

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Paste a prompt to analyze.")
        return v

    @field_validator("models")
    @classmethod
    def _dedupe(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > 12:
            raise ValueError("Pick at most 12 models.")
        return list(dict.fromkeys(v))


class TokensRequest(BaseModel):
    prompt: str = Field(..., max_length=20_000)


class Specificity(BaseModel):
    value: float
    label: str


class Check(BaseModel):
    id: str
    label: str
    passed: bool
    probability: float  # raw Jev value: noul probability or score / top level
    informational: bool = False  # shown, but does not affect the score
    detail: str


class Dimension(BaseModel):
    id: str
    label: str
    value: float  # 0–1, higher is better


class TaskType(BaseModel):
    choice: str
    confidence: float
    probabilities: dict[str, float]


class Tokens(BaseModel):
    input: int
    input_exact: bool
    output_range: tuple[int, int]


class ModelCost(BaseModel):
    model: str
    name: str
    provider: str
    tier: Tier
    input_tokens: int
    input_exact: bool
    low_usd: float
    high_usd: float
    note: str | None = None


class TipOut(BaseModel):
    id: str = ""
    signal: str
    text: str
    impact: float


class Meta(BaseModel):
    backend: str = "jev"
    degraded: bool = False
    jev_model: str
    latency_ms: int
    low_confidence: bool
    cached: bool
    caps_applied: list[str]
    uncapped_score: int
    prices_last_updated: str
    prompt_hash: str


class AnalyzeResponse(BaseModel):
    lint_score: int
    pqs_score: int
    verdict: Verdict
    first_try_success: float
    specificity: Specificity
    checks: list[Check]
    dimensions: list[Dimension]
    task_type: TaskType
    tier_hint: Tier
    suggested_model: str | None
    tokens: Tokens
    costs: list[ModelCost]
    tips: list[TipOut]
    routing: Routing | None = None
    meta: Meta


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


# ====================================================================== public API (/v1)
# Shapes follow prompt-quality-scorer.md §11, with PromptLint's lint_score alongside pqs_score.


class ScoreOptions(BaseModel):
    include_suggestions: bool = True
    include_confidence: bool = True
    store: bool = Field(False, description="Opt in to storing the prompt text to help improve scoring.")


class ScoreRequest(BaseModel):
    prompt: str = Field(..., description="The prompt about to be sent to an LLM (1–32,000 characters).")
    system: str | None = Field(None, description="Optional system prompt, used as context.")
    models: list[str] | None = Field(None, description="Model IDs from GET /v1/pricing for cost estimates.")
    backend: Backend = "auto"
    options: ScoreOptions = ScoreOptions()
    routing: RoutingOptions = RoutingOptions()

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be empty")
        return v

    @field_validator("models")
    @classmethod
    def _dedupe(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else list(dict.fromkeys(v))[:12]


class BatchItem(BaseModel):
    prompt: str
    system: str | None = None
    id: str | None = Field(None, max_length=128, description="Your own ID, echoed back.")


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(..., min_length=1)
    models: list[str] | None = None
    backend: Backend = "auto"
    options: ScoreOptions = ScoreOptions()
    routing: RoutingOptions = RoutingOptions()


class Labelled(BaseModel):
    label: str
    confidence: float
    probs: dict[str, float] | None = None


class MissingComponent(BaseModel):
    component: str
    probability: float


class Signals(BaseModel):
    clarity: float
    specificity: float
    completeness: float
    reiteration_risk: float
    first_try_success: float
    task_type: Labelled
    complexity: Labelled
    missing_components: list[str]
    missing_detail: list[MissingComponent] | None = None


class V1Scores(BaseModel):
    lint_score: int
    pqs_score: int
    verdict: Verdict


class V1Tokens(BaseModel):
    input: dict[str, int]  # tokenizer or model → count
    input_exact: dict[str, bool]
    output_p50: int
    output_p90: int
    output_range: tuple[int, int]


class CostEstimate(BaseModel):
    model: str
    name: str
    provider: str
    input_tokens: int
    input_exact: bool
    usd_p50: float
    usd_p90: float
    usd_low: float
    usd_high: float
    pricing_verified: str
    note: str | None = None


class Suggestion(BaseModel):
    id: str
    text: str
    impact: float


class BackendInfo(BaseModel):
    name: str
    version: str
    degraded: bool
    fallback_reason: str | None = None


class ScoreResponse(BaseModel):
    id: str
    overall_score: int = Field(
        ..., description="Same as scores.lint_score (PromptLint's validated composite)."
    )
    scores: V1Scores
    signals: Signals
    checks: list[Check]
    tokens: V1Tokens
    cost_estimates: list[CostEstimate]
    suggestions: list[Suggestion] | None = None
    confidence: dict[str, float] | None = None
    tier_hint: Tier
    suggested_model: str | None
    backend: BackendInfo
    routing: Routing | None = None
    stored: bool = False
    cached: bool = False
    latency_ms: int


class BatchResult(BaseModel):
    index: int
    id: str | None = None
    result: ScoreResponse | None = None
    error: dict | None = None


class BatchResponse(BaseModel):
    id: str
    results: list[BatchResult]
    scored: int
    failed: int


# ---------------------------------------------------------------- routing pipeline (/v1/route)
class RouteRequest(ScoreRequest):
    execute: bool = Field(
        True, description="Call the recommended model when you have a key for it. false = recommend only."
    )
    provider_keys: dict[Literal["anthropic", "openai", "gemini"], str] | None = Field(
        None,
        description="Per-request provider keys (never stored). Saved keys on your account are used otherwise.",
    )
    max_output_tokens: int | None = Field(None, ge=1, le=64000)
    send_anyway: bool = Field(False, description="Call a model even when routing says to clarify first.")


class Attempt(BaseModel):
    model: str
    ok: bool
    error: str | None = None


class Execution(BaseModel):
    executed: bool
    reason: str | None = None  # why it wasn't executed
    model_used: str | None = None
    provider: str | None = None
    output: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    attempts: list[Attempt] = []


class RouteResponse(ScoreResponse):
    execution: Execution


class ProviderKeyIn(BaseModel):
    provider: Adapter
    label: str | None = Field(None, max_length=80)
    api_key: str | None = Field(
        None, max_length=500, description="Optional only for keyless openai_compatible endpoints"
    )
    base_url: str | None = Field(None, max_length=500)
    model: str | None = Field(None, max_length=200)
    tier: Literal["small", "mid", "frontier"] | None = None
    input_price: float | None = Field(None, ge=0)
    output_price: float | None = Field(None, ge=0)
