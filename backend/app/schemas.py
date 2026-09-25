"""Request and response models for /api/analyze (PromptLint.md §10, plus a few display fields)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Verdict = Literal["ready_to_send", "needs_work", "likely_to_fail"]
Tier = Literal["small", "mid", "frontier"]


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to lint (1–20,000 characters).")
    models: list[str] | None = Field(None, description="Price-table model IDs; defaults apply when empty.")

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
    signal: str
    text: str
    impact: float


class Meta(BaseModel):
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
