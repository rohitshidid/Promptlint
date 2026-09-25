"""API tests with the Jev judge mocked, plus resilience tests against a faked Jev HTTP layer."""

import time

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.jev_client import JevError, JevJudge
from app.main import create_app
from app.settings import BACKEND_DIR, Settings
from app.tokens import TokenCounter
from tests.conftest import FakeJudge, load_fixture, make_answers


def settings(**kw) -> Settings:
    base = dict(
        typesafe_api_key="",
        rate_limit="1000/hour",
        tokens_rate_limit="1000/minute",
        playground_rate_limit="1000/hour",
        config_dir=BACKEND_DIR / "config",
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def fake(cfg):
    return FakeJudge(make_answers(cfg, nouls={"task_clear": 0.9, "first_try_success": 0.7}))


@pytest.fixture
def client(fake, cfg):
    app = create_app(settings(), judge=fake, counter=TokenCounter(), config=cfg)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["jev_configured"] is True


def test_models_lists_price_table(client, cfg):
    body = client.get("/api/models").json()
    assert body["last_updated"] == cfg.prices.last_updated
    assert len(body["models"]) == len(cfg.prices.models)
    assert body["default_models"] == list(cfg.prices.default_models)


def test_analyze_happy_path_matches_contract(client):
    r = client.post(
        "/api/analyze",
        json={"prompt": "Summarize this article in 3 bullets.", "models": ["claude-sonnet-5", "gpt-6-luna"]},
    )
    assert r.status_code == 200
    body = r.json()
    for key in (
        "lint_score",
        "verdict",
        "first_try_success",
        "specificity",
        "checks",
        "task_type",
        "tier_hint",
        "tokens",
        "costs",
        "tips",
        "meta",
    ):
        assert key in body
    assert body["verdict"] in {"ready_to_send", "needs_work", "likely_to_fail"}
    assert [c["model"] for c in body["costs"]] == ["claude-sonnet-5", "gpt-6-luna"]
    assert body["tokens"]["input"] > 0
    assert body["meta"]["jev_model"] == "jev-1.13.0"
    # No key for Claude token counting in tests: labeled approximate, never claimed exact.
    assert body["costs"][0]["input_exact"] is False


def test_analyze_uses_default_models(client, cfg):
    body = client.post("/api/analyze", json={"prompt": "hello"}).json()
    assert [c["model"] for c in body["costs"]] == list(cfg.prices.default_models)


def test_identical_prompts_hit_the_cache(client, fake):
    for _ in range(3):
        r = client.post("/api/analyze", json={"prompt": "same prompt"})
    assert fake.calls == 1
    assert r.json()["meta"]["cached"] is True


@pytest.mark.parametrize("prompt", ["", "   \n "])
def test_blank_prompt_is_rejected(client, prompt):
    r = client.post("/api/analyze", json={"prompt": prompt})
    assert r.status_code == 422
    assert r.json()["error"] == "Paste a prompt to analyze."


def test_oversize_prompt_is_rejected_without_calling_jev(client, fake):
    r = client.post("/api/analyze", json={"prompt": "x" * 20_001})
    assert r.status_code == 413
    assert "20,000" in r.json()["error"]
    assert fake.calls == 0


def test_unknown_model_is_rejected(client):
    r = client.post("/api/analyze", json={"prompt": "hi", "models": ["gpt-99"]})
    assert r.status_code == 422 and "gpt-99" in r.json()["error"]


def test_missing_key_returns_503(cfg):
    app = create_app(settings(), config=cfg)
    with TestClient(app) as c:
        r = c.post("/api/analyze", json={"prompt": "hello"})
    assert r.status_code == 503


def test_jev_error_is_passed_through_as_friendly_message(cfg):
    app = create_app(settings(), judge=FakeJudge(JevError(504, "The analysis took too long.")), config=cfg)
    with TestClient(app) as c:
        r = c.post("/api/analyze", json={"prompt": "hello"})
    assert r.status_code == 504 and r.json()["error"] == "The analysis took too long."


def test_rate_limit_returns_429(fake, cfg):
    app = create_app(settings(rate_limit="2/hour"), judge=fake, config=cfg)
    with TestClient(app) as c:
        codes = [c.post("/api/analyze", json={"prompt": f"p{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_token_counter_endpoint(client):
    r = client.post("/api/tokens", json={"prompt": "Hello world, this is a test."})
    assert r.status_code == 200 and r.json()["tokens"] > 0


def test_static_site_and_playground_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/app.html").status_code == 200
    assert client.get("/playground/").status_code == 200


def test_playground_blocks_foreign_origins_when_using_server_key(fake, cfg):
    app = create_app(settings(typesafe_api_key="server-key"), judge=fake, config=cfg)
    with TestClient(app) as c:
        r = c.post("/api/systemone", headers={"Origin": "https://evil.example"}, content=b"{}")
    assert r.status_code == 403


# ------------------------------------------------------------------ resilience
def _judge(cfg, handler, **kw) -> JevJudge:
    return JevJudge(
        api_key="k",
        model="jev-1.13.0",
        questions=cfg.questions,
        transport=httpx2.MockTransport(handler),
        **kw,
    )


async def test_retries_429_then_succeeds(cfg):
    body = load_fixture("strong")["response"]
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(429, json={"detail": "slow down"}, headers={"retry-after-ms": "10"})
        return httpx2.Response(200, json=body)

    answers = await _judge(cfg, handler).judge("x")
    assert calls["n"] == 2 and answers.model == body["model"]


async def test_retries_529_overloaded(cfg):
    body = load_fixture("weak")["response"]
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return (
            httpx2.Response(529, json={"detail": "overloaded"})
            if calls["n"] < 3
            else httpx2.Response(200, json=body)
        )

    answers = await _judge(cfg, handler).judge("x")
    assert calls["n"] == 3 and answers.nouls


async def test_persistent_429_becomes_friendly_503(cfg):
    judge = _judge(
        cfg,
        lambda r: httpx2.Response(429, json={"detail": "x"}, headers={"retry-after-ms": "1"}),
        max_retries=1,
    )
    with pytest.raises(JevError) as e:
        await judge.judge("x")
    assert e.value.status == 503


async def test_auth_error_is_not_retried(cfg):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx2.Response(
            401, json={"detail": {"error_type": "authentication_error", "message": "bad key"}}
        )

    with pytest.raises(JevError) as e:
        await _judge(cfg, handler).judge("x")
    assert calls["n"] == 1 and e.value.status == 500


async def test_slow_jev_times_out_without_crashing(cfg):
    def handler(request):
        raise httpx2.ReadTimeout("slow", request=request)

    start = time.perf_counter()
    with pytest.raises(JevError) as e:
        await _judge(cfg, handler, max_retries=0).judge("x")
    assert e.value.status == 504
    assert time.perf_counter() - start < 3


async def test_overall_deadline_caps_retries(cfg):
    import asyncio

    async def handler(request):
        await asyncio.sleep(0.5)
        raise httpx2.ReadTimeout("slow", request=request)

    start = time.perf_counter()
    with pytest.raises(JevError) as e:
        await _judge(cfg, handler, deadline_s=0.3, max_retries=5).judge("x")
    assert e.value.status == 504 and time.perf_counter() - start < 1.5


async def test_malformed_response_is_a_clean_error(cfg):
    with pytest.raises(JevError) as e:
        await _judge(
            cfg, lambda r: httpx2.Response(200, json={"model": "jev-1.13.0", "usage": {}, "answers": {}})
        ).judge("x")
    assert e.value.status == 502
