"""Model router, the /v1/route pipeline, saved provider keys, and provider adapters (all providers faked)."""

import asyncio
import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import router as R
from app.db import ProviderKey
from app.main import create_app
from app.providers import ProviderClient, ProviderError, check_base_url
from app.tokens import TokenCounter
from tests.conftest import FakeJudge, make_answers, make_settings

CSRF = {"X-PL-CSRF": "1"}
PASSWORD = "correct horse battery"
GOOD = {
    "task_clear": 0.95,
    "first_try_success": 0.9,
    "conflicting": 0.02,
    "has_goal": 0.9,
    "has_success_criteria": 0.9,
}


def cand(id, tier, inp, out, adapter="openai", **kw):
    return R.Candidate(
        id=id, name=id, provider=adapter, adapter=adapter, tier=tier, input=inp, output=out, **kw
    )


POOL = [
    cand("small-a", "small", 0.1, 0.5),
    cand("mid-a", "mid", 1, 4),
    cand("mid-b", "mid", 2, 10),
    cand("big-a", "frontier", 5, 25),
]


def decide(probs, strategy="balanced", verdict="needs_work", first_try=0.8, **kw):
    return R.route(
        candidates=POOL,
        complexity_probs=probs,
        input_tokens=100,
        output_p50=300,
        output_p90=900,
        verdict=verdict,
        first_try=first_try,
        missing=["goal"],
        task_type="writing",
        strategy=strategy,
        **kw,
    )


# ------------------------------------------------------------------ pure router
@pytest.mark.parametrize(
    ("probs", "strategy", "need"),
    [
        ((0.9, 0.1, 0), "balanced", "small"),
        ((0.6, 0.4, 0), "cheapest", "small"),
        ((0.6, 0.4, 0), "balanced", "mid"),
        ((0.6, 0.3, 0.1), "quality", "mid"),
        ((0.6, 0.25, 0.15), "quality", "frontier"),
        ((0, 0.2, 0.8), "cheapest", "frontier"),
    ],
)
def test_required_tier(probs, strategy, need):
    assert R.required_tier(probs, strategy) == need


def test_task_fit_moves_balanced_to_a_better_model_within_the_price_band():
    fits = {"mid-a": 0.85, "mid-b": 1.0}  # mid-b costs 2.5x mid-a here
    d = decide((0.1, 0.8, 0.1), fits=fits)
    assert d.chosen.candidate.id == "mid-b" and d.fallback.candidate.id == "mid-a"
    assert "better fit for writing" in d.reason and "2.5×" in d.reason
    # cheapest ignores fit; a tight band or a small edge keeps the cheaper model
    assert decide((0.1, 0.8, 0.1), strategy="cheapest", fits=fits).chosen.candidate.id == "mid-a"
    assert decide((0.1, 0.8, 0.1), fits=fits, price_band=2.0).chosen.candidate.id == "mid-a"
    assert decide((0.1, 0.8, 0.1), fits={"mid-a": 0.95, "mid-b": 1.0}).chosen.candidate.id == "mid-a"


def test_quality_prefers_the_best_fit_in_the_strongest_tier():
    pool = [cand("big-cheap", "frontier", 2, 12), cand("big-fit", "frontier", 4, 20)]
    d = R.route(
        candidates=pool,
        complexity_probs=(0, 0.2, 0.8),
        input_tokens=100,
        output_p50=300,
        output_p90=900,
        verdict="needs_work",
        first_try=0.8,
        missing=[],
        task_type="coding",
        strategy="quality",
        fits={"big-cheap": 0.85, "big-fit": 1.0},
    )
    assert d.chosen.candidate.id == "big-fit" and "best fit for coding" in d.reason
    assert d.task_type == "coding"


def test_routing_yaml_strengths_send_writing_to_claude(client):
    h = account(client)
    d = client.post("/v1/score", json={"prompt": "x"}, headers=h).json()
    rt = d["routing"]
    assert rt["task_type"] == "writing" and rt["recommended"]["id"] == "claude-sonnet-5"
    assert rt["fallback"]["id"] == "gemini-3.8-flash"
    fits = {m["id"]: m["task_fit"] for m in rt["alternatives"]}
    assert fits["claude-sonnet-5"] == 1.0 and fits["gemini-3.8-flash"] == 0.85


def test_simple_prompt_goes_to_the_cheapest_small_model():
    d = decide((0.95, 0.05, 0))
    assert d.chosen.candidate.id == "small-a" and d.action == "send"
    assert d.baseline.candidate.id == "big-a" and d.savings_percent > 90


def test_medium_prompt_gets_the_cheaper_mid_model_with_a_fallback():
    d = decide((0.1, 0.9, 0))
    assert d.chosen.candidate.id == "mid-a" and d.fallback.candidate.id == "mid-b"


def test_quality_prefers_the_top_tier():
    d = decide((0.1, 0.9, 0), strategy="quality")
    assert d.chosen.candidate.id == "big-a"


def test_budget_excludes_expensive_models():
    d = decide((0, 0.1, 0.9), strategy="quality", max_cost_usd=0.01)
    assert d.chosen.candidate.id != "big-a" and d.chosen.cost_p90 <= 0.01


def test_no_capable_model_uses_the_strongest_and_warns():
    d = R.route(
        candidates=POOL[:2],
        complexity_probs=(0, 0, 1),
        input_tokens=100,
        output_p50=300,
        output_p90=900,
        verdict="needs_work",
        first_try=0.8,
        missing=[],
        task_type="coding",
    )
    assert d.chosen.candidate.id == "mid-a" and d.warnings


def test_failing_prompt_says_clarify_first():
    d = decide((0.1, 0.9, 0), verdict="likely_to_fail", first_try=0.2)
    assert d.action == "clarify_first" and "goal" in d.clarify_reason


def test_explicit_baseline():
    d = decide((0.95, 0.05, 0), baseline_id="mid-b")
    assert d.baseline.candidate.id == "mid-b"
    assert d.savings_usd == pytest.approx(d.baseline.cost_p50 - d.chosen.cost_p50)


# ------------------------------------------------------------------ fake providers
class FakeProviders:
    """Records requests and answers like each provider's real API."""

    def __init__(self):
        self.requests: list[httpx2.Request] = []
        self.fail: set[str] = set()  # hosts that should return 500

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        host = request.url.host
        if host in self.fail:
            return httpx2.Response(500, json={"error": "boom"})
        body = json.loads(request.content or b"{}")
        if host == "api.anthropic.com":
            return httpx2.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": "Claude says hi"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 34},
                },
            )
        if host == "generativelanguage.googleapis.com":
            return httpx2.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "Gemini says hi"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 22},
                },
            )
        # OpenAI and every OpenAI-compatible endpoint
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": f"{host} says hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )


@pytest.fixture
def fakes():
    return FakeProviders()


def make_client(cfg, tmp_path, fakes, *, judge=None, **kw) -> TestClient:
    judge = judge or FakeJudge(
        make_answers(
            cfg, nouls=GOOD, scores={"complexity": 1, "specificity": 3, "context_given": 2, "ambiguity": 0}
        )
    )
    providers = ProviderClient(timeout_s=5, transport=httpx2.MockTransport(fakes.handler))
    app = create_app(
        make_settings(tmp_path, **kw), judge=judge, counter=TokenCounter(), config=cfg, providers=providers
    )
    return TestClient(app)


def account(c: TestClient) -> dict:
    assert (
        c.post("/v1/account/signup", json={"email": "r@x.co", "password": PASSWORD}, headers=CSRF).status_code
        == 201
    )
    key = c.post("/v1/keys", json={"name": "t"}, headers=CSRF).json()["key"]
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client(cfg, tmp_path, fakes):
    with make_client(cfg, tmp_path, fakes) as c:
        yield c


# ------------------------------------------------------------------ recommendation in /v1/score
def test_score_includes_a_routing_recommendation(client):
    h = account(client)
    d = client.post("/v1/score", json={"prompt": "Summarize this report for my boss"}, headers=h).json()
    r = d["routing"]
    assert r["strategy"] == "balanced" and r["action"] == "send"
    assert r["recommended"]["id"] == d["suggested_model"]
    assert r["recommended"]["tier"] in {"mid", "frontier"}  # medium complexity needs mid or better
    assert r["savings_percent"] >= 0 and len(r["alternatives"]) == 7


def test_routing_among_the_callers_own_models(client):
    h = account(client)
    custom = {
        "id": "llama-70b",
        "tier": "mid",
        "input_price": 0.5,
        "output_price": 0.8,
        "base_url": "https://api.groq.com/openai/v1",
    }
    d = client.post(
        "/v1/score", json={"prompt": "x", "routing": {"candidates": ["claude-haiku-4-5", custom]}}, headers=h
    ).json()
    assert d["routing"]["recommended"]["id"] == "llama-70b"
    assert {m["id"] for m in d["routing"]["alternatives"]} == {"llama-70b", "claude-haiku-4-5"}


def test_unknown_candidate_is_a_400(client):
    h = account(client)
    r = client.post("/v1/score", json={"prompt": "x", "routing": {"candidates": ["gpt-99"]}}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["type"] == "unknown_model"


# ------------------------------------------------------------------ /v1/route pipeline
def test_route_without_keys_returns_the_recommendation_only(client, fakes):
    h = account(client)
    d = client.post("/v1/route", json={"prompt": "Summarize this"}, headers=h).json()
    assert d["execution"]["executed"] is False and "No key" in d["execution"]["reason"]
    assert d["routing"]["recommended"] and not fakes.requests


def test_route_with_execute_false_never_calls_a_model(client, fakes):
    h = account(client)
    d = client.post(
        "/v1/route", json={"prompt": "x", "execute": False, "provider_keys": {"openai": "sk-test"}}, headers=h
    ).json()
    assert d["execution"]["executed"] is False and not fakes.requests


def test_route_with_a_per_request_key_calls_the_model(client, fakes):
    h = account(client)
    d = client.post(
        "/v1/route",
        json={
            "prompt": "Summarize this",
            "provider_keys": {"openai": "sk-test-123"},
            "routing": {"candidates": ["gpt-6-luna", "gpt-6-astra", "claude-sonnet-5"]},
        },
        headers=h,
    ).json()
    ex = d["execution"]
    assert ex["executed"] is True and ex["model_used"] in {"gpt-6-astra", "gpt-6-luna"}
    assert ex["output"] == "api.openai.com says hi" and ex["input_tokens"] == 10 and ex["cost_usd"] > 0
    assert fakes.requests[0].headers["Authorization"] == "Bearer sk-test-123"
    nc = d["routing"]["not_connected"]  # Sonnet was the best fit but had no key
    assert nc["model"]["id"] == "claude-sonnet-5" and nc["model"]["connected"] is False
    assert nc["instead"]["id"] == ex["model_used"] and "was used instead" in nc["note"]
    assert d["routing"]["recommended"]["connected"] is True


def test_score_says_when_the_best_fit_is_not_connected(client):
    h = account(client)
    # no connections at all: plain recommendation, no connection labels
    d = client.post("/v1/score", json={"prompt": "x"}, headers=h).json()
    assert "not_connected" not in d["routing"] and "connected" not in d["routing"]["recommended"]
    # connect only Gemini: Sonnet is still the best fit, Gemini Flash would be used instead
    r = client.post("/v1/providers", json={"provider": "gemini", "api_key": "g-key-123456"}, headers=CSRF)
    assert r.status_code == 201, r.text
    d = client.post("/v1/score", json={"prompt": "x"}, headers=h).json()
    rt = d["routing"]
    assert rt["recommended"]["id"] == "claude-sonnet-5" and rt["recommended"]["connected"] is False
    nc = rt["not_connected"]
    assert nc["instead"]["id"] == "gemini-3.8-flash" and nc["instead"]["connected"] is True
    assert "haven't connected Anthropic" in nc["note"] and "would be used instead" in nc["note"]
    flags = {m["id"]: m["connected"] for m in rt["alternatives"]}
    assert flags["gemini-3.1-pro-preview"] is True and flags["gpt-6-luna"] is False
    # the stats show the pick and what would have been used
    u = client.get("/v1/usage/routing").json()
    [key] = u["keys"]
    sonnet = next(x for x in key["recommended"] if x["model"] == "claude-sonnet-5")
    assert sonnet["not_connected"] == 1 and sonnet["instead"] == [
        {"model": "gemini-3.8-flash", "name": "Gemini 3.8 Flash", "count": 1}
    ]


def test_route_falls_back_when_the_first_model_fails(client, fakes):
    h = account(client)
    fakes.fail.add("generativelanguage.googleapis.com")  # the cheaper mid model's provider is down
    d = client.post(
        "/v1/route",
        json={
            "prompt": "x",
            "provider_keys": {"gemini": "g-1", "anthropic": "sk-ant-1"},
            "routing": {"candidates": ["gemini-3.8-flash", "claude-sonnet-5"], "strategy": "cheapest"},
        },
        headers=h,
    ).json()
    ex = d["execution"]
    assert d["routing"]["recommended"]["id"] == "gemini-3.8-flash"
    assert (
        ex["executed"] is True and ex["model_used"] == "claude-sonnet-5" and ex["output"] == "Claude says hi"
    )
    assert [a["ok"] for a in ex["attempts"]] == [False, True]


def test_route_calls_gemini_and_custom_endpoints(client, fakes):
    h = account(client)
    d = client.post(
        "/v1/route",
        json={
            "prompt": "x",
            "provider_keys": {"gemini": "g-key"},
            "routing": {"candidates": ["gemini-3.8-flash"]},
        },
        headers=h,
    ).json()
    assert d["execution"]["output"] == "Gemini says hi"
    assert fakes.requests[-1].headers["x-goog-api-key"] == "g-key"
    custom = {
        "id": "llama",
        "tier": "mid",
        "input_price": 0.1,
        "output_price": 0.2,
        "base_url": "https://llm.example.org/v1",
        "model": "llama3",
        "api_key": "k-1",
    }
    d = client.post("/v1/route", json={"prompt": "x", "routing": {"candidates": [custom]}}, headers=h).json()
    assert d["execution"]["output"] == "llm.example.org says hi"
    assert str(fakes.requests[-1].url) == "https://llm.example.org/v1/chat/completions"
    assert json.loads(fakes.requests[-1].content)["model"] == "llama3"


def test_clarify_first_is_not_sent_unless_asked(cfg, tmp_path, fakes):
    bad = FakeJudge(
        make_answers(
            cfg,
            nouls={"task_clear": 0.1, "first_try_success": 0.1},
            scores={"specificity": 0, "context_given": 0, "ambiguity": 2},
        )
    )
    with make_client(cfg, tmp_path, fakes, judge=bad) as c:
        h = account(c)
        body = {
            "prompt": "do it",
            "provider_keys": {"openai": "sk"},
            "routing": {"candidates": ["gpt-6-luna"]},
        }
        d = c.post("/v1/route", json=body, headers=h).json()
        assert d["routing"]["action"] == "clarify_first" and d["execution"]["executed"] is False
        d = c.post("/v1/route", json={**body, "send_anyway": True}, headers=h).json()
        assert d["execution"]["executed"] is True


# ------------------------------------------------------------------ saved provider keys
def test_saved_keys_are_encrypted_masked_and_used(client, fakes):
    h = account(client)
    r = client.post(
        "/v1/providers", json={"provider": "openai", "api_key": "sk-secret-abcd1234"}, headers=CSRF
    )
    assert r.status_code == 201 and r.json()["key_hint"] == "…1234"
    listed = client.get("/v1/providers").json()
    assert "sk-secret" not in json.dumps(listed) and listed["storage_enabled"] is True

    async def raw():
        async with client.app.state.sessionmaker() as s:
            return (await s.scalars(select(ProviderKey))).all()

    rows = asyncio.run(raw())
    assert rows[0].encrypted_key and "sk-secret" not in rows[0].encrypted_key
    d = client.post(
        "/v1/route", json={"prompt": "x", "routing": {"candidates": ["gpt-6-luna"]}}, headers=h
    ).json()
    assert d["execution"]["executed"] is True
    assert fakes.requests[-1].headers["Authorization"] == "Bearer sk-secret-abcd1234"


def test_saved_custom_endpoint_becomes_a_candidate(client, fakes):
    h = account(client)
    ep = {
        "provider": "openai_compatible",
        "label": "My Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "tier": "mid",
        "input_price": 0,
        "output_price": 0,
    }
    assert (
        client.post("/v1/providers", json=ep, headers=CSRF).status_code == 201
    )  # local dev allows localhost
    d = client.post("/v1/route", json={"prompt": "Summarize this"}, headers=h).json()
    assert d["routing"]["recommended"]["id"] == "llama3.1"  # free and mid-tier: best value
    assert d["execution"]["executed"] is True and d["execution"]["output"] == "localhost says hi"
    assert "Authorization" not in fakes.requests[-1].headers  # keyless endpoint


def test_private_endpoints_are_blocked_on_a_public_server(cfg, tmp_path, fakes):
    with make_client(cfg, tmp_path, fakes, allow_private_provider_urls=False) as c:
        account(c)
        for url in ("http://localhost:11434/v1", "https://127.0.0.1/v1", "https://169.254.169.254/latest"):
            r = c.post(
                "/v1/providers",
                json={
                    "provider": "openai_compatible",
                    "base_url": url,
                    "model": "m",
                    "tier": "mid",
                    "input_price": 0,
                    "output_price": 0,
                },
                headers=CSRF,
            )
            assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_base_url", url


def test_provider_keys_need_csrf_and_a_session(client):
    assert client.get("/v1/providers").status_code == 401
    account(client)
    assert client.post("/v1/providers", json={"provider": "openai", "api_key": "sk"}).status_code == 403


def test_saving_a_second_key_for_the_same_provider_replaces_it(client):
    account(client)
    client.post("/v1/providers", json={"provider": "anthropic", "api_key": "sk-ant-first-0001"}, headers=CSRF)
    client.post(
        "/v1/providers", json={"provider": "anthropic", "api_key": "sk-ant-second-0002"}, headers=CSRF
    )
    rows = client.get("/v1/providers").json()["providers"]
    assert [r["key_hint"] for r in rows] == ["…0002"]


def test_delete_provider_and_account_removes_keys(client):
    account(client)
    pid = client.post(
        "/v1/providers", json={"provider": "gemini", "api_key": "g-key-000000"}, headers=CSRF
    ).json()["id"]
    assert client.delete(f"/v1/providers/{pid}", headers=CSRF).status_code == 204
    client.post("/v1/providers", json={"provider": "gemini", "api_key": "g-key-000000"}, headers=CSRF)
    assert client.post("/v1/account/delete", json={"password": PASSWORD}, headers=CSRF).status_code == 204

    async def count():
        async with client.app.state.sessionmaker() as s:
            return len((await s.scalars(select(ProviderKey))).all())

    assert asyncio.run(count()) == 0


def test_storage_is_disabled_without_a_secret_in_production(cfg, tmp_path, fakes):
    # A non-SQLite URL means "production": no dev secret fallback. Checked on the settings object only.
    s = make_settings(tmp_path, database_url="postgresql+asyncpg://u:p@h/db")
    assert s.effective_provider_key_secret == "" and s.private_provider_urls_allowed is False


# ------------------------------------------------------------------ adapters directly
def test_check_base_url_rules():
    assert (
        check_base_url("https://api.groq.com/openai/v1/", allow_private=True)
        == "https://api.groq.com/openai/v1"
    )
    for bad in ("ftp://x", "https://user:pw@host/v1", "not a url"):
        with pytest.raises(ProviderError):
            check_base_url(bad, allow_private=True)
    with pytest.raises(ProviderError):
        check_base_url("http://api.groq.com/v1", allow_private=False)  # https required in production


async def test_bad_key_is_reported_not_retried(fakes):
    def deny(request):
        return httpx2.Response(401, json={"error": "invalid key"})

    pc = ProviderClient(timeout_s=5, transport=httpx2.MockTransport(deny))
    with pytest.raises(ProviderError) as e:
        await pc.complete(
            adapter="openai", model="gpt-6-luna", prompt="x", system=None, api_key="bad", max_output_tokens=10
        )
    assert e.value.status == 401 and e.value.retryable is False
    await pc.aclose()


async def test_anthropic_adapter_sends_system_and_reads_usage(fakes):
    pc = ProviderClient(timeout_s=5, transport=httpx2.MockTransport(fakes.handler))
    out = await pc.complete(
        adapter="anthropic",
        model="claude-sonnet-5",
        prompt="hi",
        system="Be brief.",
        api_key="sk-ant",
        max_output_tokens=100,
    )
    body = json.loads(fakes.requests[-1].content)
    assert out.text == "Claude says hi" and out.input_tokens == 12 and out.output_tokens == 34
    assert body["system"] == "Be brief." and body["max_tokens"] == 100
    assert fakes.requests[-1].headers["x-api-key"] == "sk-ant"
    await pc.aclose()


# ------------------------------------------------------------------ routing stats on the account page
def test_routing_usage_per_key_and_model(client, fakes):
    h = account(client)
    # one recommendation-only check, and one real call to Claude with a per-request key
    client.post("/v1/score", json={"prompt": "x"}, headers=h)
    d = client.post(
        "/v1/route",
        json={"prompt": "x", "provider_keys": {"anthropic": "sk-ant-1"}},
        headers=h,
    ).json()
    assert d["execution"]["executed"] is True and d["execution"]["model_used"] == "claude-sonnet-5"

    u = client.get("/v1/usage/routing").json()  # signed-in session
    t = u["totals"]
    assert t["checks"] == 2 and t["sent"] == 1 and t["failed"] == 0
    assert t["spent_usd"] == d["execution"]["cost_usd"]
    # the baseline for the real call is the priciest model with a key (Claude Opus 5.5), same tokens
    assert t["saved_usd"] > 0 and t["est_saved_usd"] > 0
    [key] = u["keys"]
    assert key["checks"] == 2 and key["providers_used"] == ["Anthropic"]
    assert key["sent_to"] == [
        {"model": "claude-sonnet-5", "name": "Claude Sonnet 5", "calls": 1, "spent_usd": t["spent_usd"]}
    ]
    assert key["recommended"][0] == {
        "model": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "count": 2,
        "not_connected": 0,
        "instead": [],
    }
    sonnet = next(m for m in u["models"] if m["model"] == "claude-sonnet-5")
    assert sonnet["recommended"] == 2 and sonnet["sent"] == 1

    # with an API key, only that key's numbers; without auth, 401
    assert client.get("/v1/usage/routing", headers=h).json()["totals"]["checks"] == 2
    client.cookies.clear()
    assert client.get("/v1/usage/routing").status_code == 401
