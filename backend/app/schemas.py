"""Request and response models for /api/analyze (PromptLint.md §10, plus a few display fields)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Verdict = Literal["ready_to_send", "needs_work", "likely_to_fail"]
Tier = Literal["small", "mid", "frontier"]


Backend = Literal["auto", "jev", "heuristic"]


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to lint (1–20,000 characters).")
    models: list[str] | None = Field(None, description="Price-table model IDs; defaults apply when empty.")
    backend: Backend = Field("auto", description="auto = Jev with heuristic fallback")

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
