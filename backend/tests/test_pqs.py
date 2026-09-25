"""PQS composite, missing components, output quantiles, heuristic backend, security primitives, DB URLs."""

import pytest

from app import scoring
from app.backends import BackendRouter, HeuristicJudge
from app.db import normalize_url
from app.jev_client import JevError
from app.security import (
    hash_password,
    new_api_key,
    parse_api_key,
    prompt_digest,
    verify_password,
    verify_secret,
)
from tests.conftest import FakeJudge, make_answers

ALL_GOOD = {
    "task_clear": 1.0,
    "first_try_success": 1.0,
    "has_goal": 1.0,
    "has_constraints": 1.0,
    "has_output_format": 1.0,
    "has_audience": 1.0,
    "has_examples": 1.0,
    "has_success_criteria": 1.0,
}


def pqs_of(cfg, task_type="writing", **kw):
    a = make_answers(cfg, task_type=task_type, **kw)
    return scoring.pqs_score(scoring.raw_values(a), task_type, cfg.pqs)


def test_pqs_weights_sum_to_one(cfg):
    assert sum(cfg.pqs.weights.values()) == pytest.approx(1.0)


def test_perfect_prompt_pqs_100(cfg):
    r = pqs_of(cfg, nouls=ALL_GOOD, scores={"specificity": 3, "context_given": 2, "ambiguity": 0})
    assert r.pqs_score == 100 and r.completeness == 1.0 and r.reiteration_risk == 0.0
    assert all(p == 0.0 for p in r.missing.values())


def test_empty_prompt_pqs_0(cfg):
    bad = {k: 0.0 for k in ALL_GOOD}
    r = pqs_of(cfg, nouls=bad, scores={"specificity": 0, "context_given": 0, "ambiguity": 2})
    assert r.pqs_score == 0 and r.completeness == 0.0 and r.reiteration_risk == 1.0


def test_pqs_formula_matches_the_design_doc(cfg):
    # clarity = mean(task_clear, 1 − ambiguity) = mean(1.0, 0.5) = 0.75; specificity 1/3;
    # completeness 1.0 (nothing missing); reiteration 1 − 0.6 = 0.4.
    nouls = dict(ALL_GOOD, first_try_success=0.6)
    r = pqs_of(cfg, nouls=nouls, scores={"specificity": 1, "context_given": 2, "ambiguity": 1})
    expected = 100 * (0.35 * 0.75 + 0.25 * (1 / 3) + 0.25 * 1.0 + 0.15 * 0.6)
    assert r.pqs_score == round(expected)


def test_task_type_changes_component_weights(cfg):
    # Missing only the output format hurts extraction more than brainstorming (pqs_scoring.yaml).
    nouls = dict(ALL_GOOD, has_output_format=0.0)
    extract = pqs_of(
        cfg,
        "extraction_transformation",
        nouls=nouls,
        scores={"specificity": 3, "context_given": 2, "ambiguity": 0},
    )
    brainstorm = pqs_of(
        cfg, "brainstorming", nouls=nouls, scores={"specificity": 3, "context_given": 2, "ambiguity": 0}
    )
    assert extract.completeness < brainstorm.completeness


RANGES = ((10, 60), (60, 300), (300, 900), (900, 3000))


@pytest.mark.parametrize(
    ("probs", "q", "expected"),
    [
        ((0, 0, 1, 0), 0.5, 600),  # midpoint of the one certain bucket
        ((0.5, 0.5, 0, 0), 0.5, 60),  # CDF crosses 0.5 exactly at the bucket edge
        ((0.25, 0.25, 0.25, 0.25), 0.9, 2160),  # 0.9 is 60% into the last bucket
    ],
)
def test_output_quantiles(probs, q, expected):
    assert scoring.output_quantile(probs, RANGES, q) == expected


def test_p50_never_exceeds_p90():
    for probs in [(1, 0, 0, 0), (0.1, 0.2, 0.3, 0.4), (0, 0, 0, 1), (0.7, 0, 0, 0.3)]:
        assert scoring.output_quantile(probs, RANGES, 0.5) <= scoring.output_quantile(probs, RANGES, 0.9)


# ------------------------------------------------------------------ heuristic backend
async def test_heuristic_answers_every_question(cfg):
    a = await HeuristicJudge(cfg.questions).judge(
        "Write a Python function that sorts a list. Return only the code."
    )
    assert set(a.nouls) == set(cfg.questions.nouls)
    assert set(a.scores) == set(cfg.questions.scores)
    assert a.choices["task_type"].choice in cfg.questions.choices["task_type"][1]
    assert a.model == "heuristic-1.0"
    for s in a.scores.values():
        assert sum(s.probabilities) == pytest.approx(1.0)


async def test_heuristic_ranks_specific_above_vague(cfg):
    h = HeuristicJudge(cfg.questions)
    vague = await h.judge("write me a poem")
    specific = await h.judge(
        "I'm a teacher. Write a 12-line rhyming poem for my Year 10 class about ionic bonding, "
        "using one simple analogy, and end with a two-line summary. Return only the poem."
    )
    lint = lambda a: scoring.lint_score(scoring.raw_values(a), cfg.weights).lint_score  # noqa: E731
    assert lint(specific) > lint(vague) + 20


async def test_heuristic_flags_contradictions(cfg):
    a = await HeuristicJudge(cfg.questions).judge(
        "Write a detailed 2,000-word essay. Keep it under 100 words."
    )
    assert a.nouls["conflicting"] > 0.7


async def test_router_modes(cfg):
    ok = FakeJudge(make_answers(cfg))
    down = FakeJudge(JevError(503, "down"))
    h = HeuristicJudge(cfg.questions)
    assert (await BackendRouter(jev=ok, heuristic=h).judge("x")).backend == "jev"
    r = await BackendRouter(jev=down, heuristic=h).judge("x")
    assert r.backend == "heuristic" and r.degraded and r.fallback_reason == "jev_503"
    with pytest.raises(JevError):
        await BackendRouter(jev=down, heuristic=h).judge("x", backend="jev")
    r = await BackendRouter(jev=None, heuristic=h).judge("x")
    assert r.degraded and r.fallback_reason == "jev_not_configured"
    assert (await BackendRouter(jev=ok, heuristic=h).judge("x", backend="heuristic")).degraded is False


# ------------------------------------------------------------------ security
def test_api_key_round_trip():
    full, prefix, secret_hash = new_api_key()
    assert full.startswith(f"pqs_live_{prefix}_") and len(prefix) == 8
    parsed = parse_api_key(full)
    assert parsed is not None and parsed[0] == prefix
    assert verify_secret(parsed[1], secret_hash)
    assert not verify_secret(parsed[1] + "x", secret_hash)
    assert secret_hash not in full  # only the hash is stored


def test_api_keys_are_unique():
    assert len({new_api_key()[0] for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "bad", ["", "pqs_live_", "sk-abc", "pqs_live_short_secret", "pqs_test_AAAAAAAA_" + "b" * 43]
)
def test_malformed_keys_are_rejected(bad):
    assert parse_api_key(bad) is None


def test_password_hashing():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert verify_password("correct horse battery", h)
    assert not verify_password("correct horse batterY", h)
    assert not verify_password("anything", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_prompt_digest_is_salted():
    assert prompt_digest("hi", None, "a") != prompt_digest("hi", None, "b")
    assert prompt_digest("hi", None, "a") != prompt_digest("hi", "sys", "a")


# ------------------------------------------------------------------ database URLs
@pytest.mark.parametrize(
    ("url", "async_url", "ssl"),
    [
        (
            "postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x.neon.tech/db",
            True,
        ),
        ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db", False),
        ("sqlite+aiosqlite:///tmp/x.db", "sqlite+aiosqlite:///tmp/x.db", False),
    ],
)
def test_normalize_url(url, async_url, ssl):
    got, args = normalize_url(url)
    assert got == async_url
    assert args.get("ssl", False) is ssl


def test_migrations_upgrade_a_fresh_database(tmp_path):
    import sqlite3

    from app.main import run_migrations

    db = tmp_path / "m.db"
    run_migrations(f"sqlite+aiosqlite:///{db}")
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master where type='table'")}
    assert {
        "users",
        "sessions",
        "api_keys",
        "usage_daily",
        "score_events",
        "stored_prompts",
        "alembic_version",
    } <= tables
