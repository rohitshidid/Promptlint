import json
from pathlib import Path

import httpx2
import pytest

from app.config import AppConfig, load_config
from app.jev_client import ChoiceResult, JevAnswers, JevJudge, ScoreResult
from app.settings import BACKEND_DIR

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def cfg() -> AppConfig:
    return load_config(BACKEND_DIR / "config")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"jev_{name}.json").read_text())


def mock_transport(handler) -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)


def fixture_judge(cfg: AppConfig, name: str, **kw) -> JevJudge:
    """A real JevJudge whose HTTP layer replays a recorded Jev response."""
    body = load_fixture(name)["response"]
    return JevJudge(
        api_key="test-key",
        model="jev-1.13.0",
        questions=cfg.questions,
        transport=mock_transport(lambda request: httpx2.Response(200, json=body)),
        **kw,
    )


def make_answers(
    cfg: AppConfig,
    *,
    nouls: dict[str, float] | None = None,
    scores: dict[str, float] | None = None,
    confidence: float = 0.9,
    expected_length_probs: tuple[float, ...] | None = None,
    task_type: str = "writing",
) -> JevAnswers:
    """Synthetic Jev answers: every noul defaults to 0.5, every score to its midpoint."""
    n = {k: 0.5 for k in cfg.questions.nouls}
    n.update(nouls or {})
    s: dict[str, ScoreResult] = {}
    for name, q in cfg.questions.scores.items():
        value = (scores or {}).get(name, q.top / 2)
        probs = [0.0] * (q.top + 1)
        probs[round(value)] = 1.0
        if name == "expected_length" and expected_length_probs is not None:
            probs = list(expected_length_probs)
        s[name] = ScoreResult(value, confidence, tuple(probs), q.top)
    labels = cfg.questions.choices["task_type"][1]
    choice = ChoiceResult(task_type, confidence, {k: (1.0 if k == task_type else 0.0) for k in labels})
    return JevAnswers(nouls=n, scores=s, choices={"task_type": choice}, model="jev-1.13.0", latency_ms=123)


class FakeJudge:
    """Returns canned answers; counts calls so tests can check caching."""

    def __init__(self, answers: JevAnswers | Exception):
        self.answers = answers
        self.calls = 0

    async def judge(self, prompt: str) -> JevAnswers:
        self.calls += 1
        if isinstance(self.answers, Exception):
            raise self.answers
        return self.answers
