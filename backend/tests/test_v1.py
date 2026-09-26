"""Public API (/v1) and accounts: the full self-serve flow, auth, limits, quotas and error shapes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import ScoreEvent, StoredPrompt
from app.jev_client import JevError
from app.main import create_app
from app.tokens import TokenCounter
from tests.conftest import FakeJudge, make_answers, make_settings

CSRF = {"X-PL-CSRF": "1"}
PASSWORD = "correct horse battery"


@pytest.fixture
def fake(cfg):
    return FakeJudge(
        make_answers(cfg, nouls={"task_clear": 0.9, "first_try_success": 0.7}, task_type="coding")
    )


def make_client(cfg, tmp_path, judge=None, **settings_kw) -> TestClient:
    app = create_app(make_settings(tmp_path, **settings_kw), judge=judge, counter=TokenCounter(), config=cfg)
    return TestClient(app)


def signup(c: TestClient, email="dev@example.com") -> dict:
    r = c.post("/v1/account/signup", json={"email": email, "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


def new_key(c: TestClient) -> str:
    r = c.post("/v1/keys", json={"name": "test"}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()["key"]


@pytest.fixture
def client(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake) as c:
        yield c


@pytest.fixture
def auth(client) -> dict:
    signup(client)
    return {"Authorization": f"Bearer {new_key(client)}"}


# ------------------------------------------------------------------ scoring
def test_score_returns_pqs_shape_with_both_scores(client, auth):
    r = client.post("/v1/score", json={"prompt": "fix my code", "models": ["claude-sonnet-5"]}, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"].startswith("scr_")
    assert d["overall_score"] == d["scores"]["lint_score"]
    assert 0 <= d["scores"]["pqs_score"] <= 100
    s = d["signals"]
    for k in ("clarity", "specificity", "completeness", "reiteration_risk"):
        assert 0 <= s[k] <= 1
    assert s["task_type"]["label"] == "coding"
    assert s["complexity"]["label"] in {"low", "medium", "high"}
    assert set(s["complexity"]["probs"]) == {"low", "medium", "high"}
    assert d["tokens"]["output_p50"] <= d["tokens"]["output_p90"]
    assert d["tokens"]["input"]["o200k_base"] > 0
    assert d["cost_estimates"][0]["usd_p50"] <= d["cost_estimates"][0]["usd_p90"]
    assert d["backend"] == {"name": "jev", "version": "jev-1.13.0", "degraded": False}
    assert r.headers["X-Request-Id"].startswith("req_")


def test_system_prompt_is_passed_to_the_backend(client, auth, fake):
    client.post("/v1/score", json={"prompt": "summarize this", "system": "You are terse."}, headers=auth)
    assert fake.systems[-1] == "You are terse."


def test_options_can_drop_suggestions_and_confidence(client, auth):
    d = client.post(
        "/v1/score",
        json={"prompt": "hello", "options": {"include_suggestions": False, "include_confidence": False}},
        headers=auth,
    ).json()
    assert "suggestions" not in d and "confidence" not in d
    assert "probs" not in d["signals"]["task_type"]


def test_jev_down_falls_back_to_heuristic_and_says_so(cfg, tmp_path):
    with make_client(cfg, tmp_path, judge=FakeJudge(JevError(503, "busy"))) as c:
        signup(c)
        h = {"Authorization": f"Bearer {new_key(c)}"}
        d = c.post("/v1/score", json={"prompt": "write me a poem"}, headers=h).json()
        assert d["backend"]["name"] == "heuristic" and d["backend"]["degraded"] is True
        assert d["backend"]["fallback_reason"] == "jev_503"
        forced = c.post("/v1/score", json={"prompt": "write me a poem", "backend": "jev"}, headers=h)
        assert forced.status_code == 503 and forced.json()["error"]["type"] == "backend_unavailable"


def test_batch_scores_items_and_reports_per_item_errors(client, auth):
    items = [
        {"prompt": "fix my code", "id": "a"},
        {"prompt": "   ", "id": "b"},
        {"prompt": "x" * 32_001, "id": "c"},
    ]
    d = client.post("/v1/score/batch", json={"items": items}, headers=auth).json()
    assert d["scored"] == 1 and d["failed"] == 2
    by_id = {r["id"]: r for r in d["results"]}
    assert by_id["a"]["result"]["overall_score"] >= 0
    assert by_id["b"]["error"]["type"] == "invalid_request"
    assert by_id["c"]["error"]["type"] == "prompt_too_long"


def test_batch_size_is_limited_by_plan(client, auth):
    r = client.post("/v1/score/batch", json={"items": [{"prompt": "hi"}] * 11}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["type"] == "batch_too_large"


def test_store_is_opt_in(client, auth):
    import asyncio

    client.post("/v1/score", json={"prompt": "not stored"}, headers=auth)
    client.post("/v1/score", json={"prompt": "please store me", "options": {"store": True}}, headers=auth)

    async def rows():
        async with client.app.state.sessionmaker() as s:
            events = (await s.scalars(select(ScoreEvent))).all()
            stored = (await s.scalars(select(StoredPrompt))).all()
            return events, stored

    events, stored = asyncio.run(rows())
    assert len(events) == 2
    assert all("store" not in e.prompt_hash and len(e.prompt_hash) == 64 for e in events)  # hash only
    assert [p.prompt for p in stored] == ["please store me"]


# ------------------------------------------------------------------ auth and errors
def test_missing_and_bad_keys(client):
    r = client.post("/v1/score", json={"prompt": "hi"})
    assert r.status_code == 401 and r.json()["error"]["type"] == "missing_api_key"
    assert r.json()["error"]["request_id"].startswith("req_")
    r = client.post("/v1/score", json={"prompt": "hi"}, headers={"Authorization": "Bearer nope"})
    assert r.json()["error"]["type"] == "invalid_api_key"
    r = client.post(
        "/v1/score", json={"prompt": "hi"}, headers={"Authorization": "Bearer pqs_live_AAAAAAAA_" + "b" * 43}
    )
    assert r.status_code == 401 and r.json()["error"]["type"] == "invalid_api_key"


def test_revoked_key_stops_working(client, auth):
    kid = client.get("/v1/keys").json()["keys"][0]["id"]
    assert client.delete(f"/v1/keys/{kid}", headers=CSRF).status_code == 204
    r = client.post("/v1/score", json={"prompt": "hi"}, headers=auth)
    assert r.status_code == 401


def test_validation_errors_are_400_in_pqs_shape(client, auth):
    r = client.post("/v1/score", json={"prompt": ""}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request"
    r = client.post("/v1/score", json={"prompt": "hi", "models": ["gpt-99"]}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["type"] == "unknown_model"
    r = client.post("/v1/score", json={"prompt": "x" * 32_001}, headers=auth)
    assert r.status_code == 413 and r.json()["error"]["type"] == "prompt_too_long"


def test_rate_limit_headers_and_429(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake) as c:
        signup(c)
        h = {"Authorization": f"Bearer {new_key(c)}"}
        rpm = cfg.plans.get("free").rpm
        codes = []
        for i in range(rpm + 1):
            r = c.post("/v1/score", json={"prompt": f"p{i}"}, headers=h)
            codes.append(r.status_code)
        assert codes[:rpm] == [200] * rpm and codes[rpm] == 429
        assert r.json()["error"]["type"] == "rate_limit_exceeded"
        assert r.headers["X-RateLimit-Limit"] == str(rpm) and r.headers["X-RateLimit-Remaining"] == "0"
        assert int(r.headers["Retry-After"]) >= 1


def test_daily_quota_is_enforced_account_wide(cfg, tmp_path, fake, monkeypatch):
    from app.config import Plan

    small = Plan(name="free", rpm=100, daily=3, monthly=None, batch_max=10)
    monkeypatch.setitem(cfg.plans.plans, "free", small)
    with make_client(cfg, tmp_path, judge=fake) as c:
        signup(c)
        k1 = {"Authorization": f"Bearer {new_key(c)}"}
        k2 = {"Authorization": f"Bearer {new_key(c)}"}
        assert c.post("/v1/score", json={"prompt": "a"}, headers=k1).headers["X-Quota-Remaining"] == "2"
        assert (
            c.post(
                "/v1/score/batch", json={"items": [{"prompt": "b"}, {"prompt": "c"}]}, headers=k2
            ).status_code
            == 200
        )
        r = c.post("/v1/score", json={"prompt": "d"}, headers=k1)
        assert r.status_code == 429 and r.json()["error"]["type"] == "quota_exceeded"


def test_usage_by_key_and_by_account(client, auth):
    client.post("/v1/score", json={"prompt": "a"}, headers=auth)
    client.post("/v1/score/batch", json={"items": [{"prompt": "b"}, {"prompt": "c"}]}, headers=auth)
    by_key = client.get("/v1/usage?days=7", headers=auth).json()
    assert by_key["scope"] == "key" and by_key["used_today"] == 3
    assert by_key["days"][-1]["requests"] == 2 and by_key["days"][-1]["prompts"] == 3
    assert len(by_key["days"]) == 7
    account = client.get("/v1/usage").json()  # session cookie
    assert account["scope"] == "account" and account["plan"]["name"] == "free"


def test_public_endpoints(client):
    p = client.get("/v1/pricing").json()
    assert p["last_verified"] and len(p["models"]) == 7
    h = client.get("/v1/health").json()
    assert h["status"] == "ok" and h["database"] == "ok" and h["backends"]["jev"]["configured"] is True


# ------------------------------------------------------------------ accounts
def test_signup_login_logout_cycle(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake) as c:
        me = signup(c, "Mixed.Case@Example.com")
        assert me["email"] == "mixed.case@example.com" and me["plan"]["name"] == "free"
        assert c.post("/v1/account/logout", headers=CSRF).status_code == 204
        assert c.get("/v1/account/me").status_code == 401
        bad = c.post(
            "/v1/account/login",
            json={"email": "mixed.case@example.com", "password": "wrong password!"},
            headers=CSRF,
        )
        assert bad.status_code == 401 and bad.json()["error"]["type"] == "invalid_credentials"
        ok = c.post(
            "/v1/account/login", json={"email": "MIXED.case@example.com", "password": PASSWORD}, headers=CSRF
        )
        assert ok.status_code == 200 and c.get("/v1/account/me").status_code == 200


def test_signup_validation(client):
    r = client.post("/v1/account/signup", json={"email": "nope", "password": PASSWORD}, headers=CSRF)
    assert r.json()["error"]["type"] == "invalid_email"
    r = client.post("/v1/account/signup", json={"email": "a@b.co", "password": "short"}, headers=CSRF)
    assert r.json()["error"]["type"] == "weak_password"
    signup(client, "a@b.co")
    r = client.post("/v1/account/signup", json={"email": "A@B.co", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 409 and r.json()["error"]["type"] == "email_taken"


def test_session_cookie_is_httponly(client):
    r = client.post("/v1/account/signup", json={"email": "c@d.co", "password": PASSWORD}, headers=CSRF)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_cookie_writes_need_csrf_header_and_same_origin(client):
    signup(client)
    assert client.post("/v1/keys", json={"name": "x"}).json()["error"]["type"] == "csrf_failed"
    r = client.post("/v1/keys", json={"name": "x"}, headers={**CSRF, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert (
        client.post("/v1/account/signup", json={"email": "e@f.co", "password": PASSWORD}).status_code == 403
    )


def test_full_key_is_shown_once(client):
    signup(client)
    created = client.post("/v1/keys", json={"name": "ci"}, headers=CSRF).json()
    assert created["key"].startswith("pqs_live_" + created["prefix"] + "_")
    listed = client.get("/v1/keys").json()["keys"][0]
    assert "key" not in listed and listed["masked"].endswith("_••••")


def test_key_limit_per_account(client):
    signup(client)
    for _ in range(5):
        new_key(client)
    r = client.post("/v1/keys", json={"name": "sixth"}, headers=CSRF)
    assert r.status_code == 400 and r.json()["error"]["type"] == "too_many_keys"


def test_users_cannot_touch_each_others_keys(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake) as a, make_client(cfg, tmp_path, judge=fake) as b:
        signup(a, "a@x.co")
        new_key(a)
        signup(b, "b@x.co")
        a_key_id = a.get("/v1/keys").json()["keys"][0]["id"]
        assert b.delete(f"/v1/keys/{a_key_id}", headers=CSRF).status_code == 404


def test_delete_account_removes_everything(client, auth):
    client.post("/v1/score", json={"prompt": "a", "options": {"store": True}}, headers=auth)
    r = client.post("/v1/account/delete", json={"password": "wrong password!"}, headers=CSRF)
    assert r.status_code == 401
    assert client.post("/v1/account/delete", json={"password": PASSWORD}, headers=CSRF).status_code == 204
    assert client.post("/v1/score", json={"prompt": "a"}, headers=auth).status_code == 401
    assert (
        client.post(
            "/v1/account/login", json={"email": "dev@example.com", "password": PASSWORD}, headers=CSRF
        ).status_code
        == 401
    )


def test_signup_is_rate_limited_per_ip(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake, signup_rate_limit="2/hour") as c:
        codes = [
            c.post(
                "/v1/account/signup", json={"email": f"u{i}@x.co", "password": PASSWORD}, headers=CSRF
            ).status_code
            for i in range(3)
        ]
    assert codes == [201, 201, 429]


def test_turnstile_is_enforced_when_configured(cfg, tmp_path, fake):
    with make_client(cfg, tmp_path, judge=fake, turnstile_secret_key="s", turnstile_site_key="k") as c:
        assert c.get("/v1/account/config").json()["turnstile_site_key"] == "k"
        r = c.post("/v1/account/signup", json={"email": "t@x.co", "password": PASSWORD}, headers=CSRF)
        assert r.status_code == 400 and r.json()["error"]["type"] == "captcha_required"


def test_empty_bearer_explains_the_shell_dollar_trap(client):
    # `Bearer $pqs_live_…` in a shell expands to an empty string.
    r = client.post("/v1/score", json={"prompt": "hi"}, headers={"Authorization": "Bearer "})
    assert r.status_code == 401 and "remove the $" in r.json()["error"]["message"]


def test_bare_bearer_word_gets_the_same_hint(client):
    r = client.post("/v1/score", json={"prompt": "hi"}, headers={"Authorization": "Bearer"})
    assert r.status_code == 401 and "remove the $" in r.json()["error"]["message"]


def test_public_api_allows_cross_origin_calls(client, auth):
    # e.g. a tester page opened from disk (Origin: null) or another site calling with its own key
    pre = client.options(
        "/v1/score",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert pre.status_code == 204 and pre.headers["Access-Control-Allow-Origin"] == "*"
    assert "authorization" in pre.headers["Access-Control-Allow-Headers"].lower()
    r = client.post("/v1/score", json={"prompt": "hi"}, headers={**auth, "Origin": "null"})
    assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "*"
    assert "X-RateLimit-Remaining" in r.headers["Access-Control-Expose-Headers"]
    err = client.post("/v1/score", json={"prompt": "hi"}, headers={"Origin": "null"})
    assert err.status_code == 401 and err.headers["Access-Control-Allow-Origin"] == "*"  # errors readable too


def test_cookie_endpoints_stay_same_origin(client):
    signup(client)
    for path in ("/v1/keys", "/v1/account/me"):
        r = client.get(path, headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in r.headers
        assert "Access-Control-Allow-Credentials" not in r.headers
    pre = client.options(
        "/v1/keys", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "Access-Control-Allow-Origin" not in pre.headers
