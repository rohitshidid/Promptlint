"""The one Jev call: sends every question in a single system_one request and normalizes the answers.

Everything downstream (scoring, cost, tips) works on `JevAnswers`, never on SDK types, so tests can
feed synthetic answers and the eval scripts can reuse the same path.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from typesafe_sdk import (
    AsyncTypeSafeClient,
    ChoiceAnswer,
    NoulAnswer,
    RetryPolicy,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from app.config import QuestionConfig
from app.questions import build_questions

log = logging.getLogger("promptlint.jev")


@dataclass(frozen=True)
class ScoreResult:
    score: float  # expected level, 0..top
    confidence: float
    probabilities: tuple[float, ...]  # index = level
    top: int


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class JevAnswers:
    nouls: dict[str, float]
    scores: dict[str, ScoreResult]
    choices: dict[str, ChoiceResult]
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class JevError(Exception):
    """A Jev failure, carrying the HTTP status and a message that is safe to show users."""

    def __init__(self, status: int, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class Judge(Protocol):
    """A scoring backend (PQS §5): turns a prompt into the same typed answers Jev returns."""

    name: str

    async def judge(self, prompt: str, system: str | None = None) -> JevAnswers: ...


def parse_response(resp: SystemOneResponse, cfg: QuestionConfig, latency_ms: int) -> JevAnswers:
    """Normalize an SDK response. Raises JevError if an expected answer is missing or mistyped."""
    nouls: dict[str, float] = {}
    scores: dict[str, ScoreResult] = {}
    choices: dict[str, ChoiceResult] = {}
    answers = resp.answers

    for name in cfg.nouls:
        a = answers.get(name)
        if not isinstance(a, NoulAnswer):
            raise JevError(502, f"Jev returned no yes/no answer for {name!r}.")
        nouls[name] = float(a.noul)

    for name, q in cfg.scores.items():
        a = answers.get(name)
        if not isinstance(a, ScoreAnswer):
            raise JevError(502, f"Jev returned no score for {name!r}.")
        probs = tuple(float(a.probabilities.get(i, 0.0)) for i in range(q.top + 1))
        scores[name] = ScoreResult(float(a.score), float(a.confidence), probs, q.top)

    for name in cfg.choices:
        a = answers.get(name)
        if not isinstance(a, ChoiceAnswer):
            raise JevError(502, f"Jev returned no choice for {name!r}.")
        choices[name] = ChoiceResult(a.choice, float(a.confidence), dict(a.probabilities))

    return JevAnswers(
        nouls=nouls,
        scores=scores,
        choices=choices,
        model=resp.model,
        latency_ms=latency_ms,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )


class JevJudge:
    """Production judge backed by the TypeSafe SDK."""

    name = "jev"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        questions: QuestionConfig,
        timeout_s: float = 2.0,
        deadline_s: float = 6.0,
        max_retries: int = 2,
        transport=None,  # httpx2 transport; tests inject a MockTransport
    ):
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY is not set")
        self._cfg = questions
        self._questions = build_questions(questions)
        self._model = model
        self._deadline_s = deadline_s
        # 429 and 5xx (including 529 overloaded) are retried with backoff; auth errors are not.
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            model=model,
            timeout=timeout_s,
            transport=transport,
            retry=RetryPolicy(
                max_retries=max_retries,
                backoff_initial=0.25,
                backoff_max=1.5,
                http_statuses={408, 429, *range(500, 600)},
            ),
        )

    async def judge(self, prompt: str, system: str | None = None) -> JevAnswers:
        # The system prompt rides along as context; every question still asks about `prompt`.
        state = {"prompt": prompt} if not system else {"prompt": prompt, "system": system}
        start = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._client.system_one(state=state, questions=self._questions),
                timeout=self._deadline_s,
            )
        except (TimeoutError, TypeSafeAPITimeoutError) as e:
            raise JevError(504, "The analysis took too long. Please try again in a moment.") from e
        except TypeSafeRateLimitError as e:
            raise JevError(503, "The analysis service is busy right now. Please try again shortly.") from e
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as e:
            log.error("Jev rejected the API key: %s", e)
            raise JevError(500, "The server's analysis key is misconfigured.") from e
        except TypeSafeAPIConnectionError as e:
            raise JevError(503, "Couldn't reach the analysis service. Please try again.") from e
        except TypeSafeError as e:
            log.exception("Jev call failed")
            raise JevError(502, "The analysis service returned an error. Please try again.") from e
        latency_ms = round((time.perf_counter() - start) * 1000)
        answers = parse_response(resp, self._cfg, latency_ms)
        if answers.model != self._model and not self._model.endswith("-latest"):
            log.warning("Jev served %s but %s is pinned", answers.model, self._model)
        return answers

    async def aclose(self) -> None:
        await self._client.aclose()
