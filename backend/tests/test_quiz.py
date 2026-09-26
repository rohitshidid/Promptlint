"""Quiz: rounds, server-side scoring, relevance guard, grades and leaderboard."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.quiz import heuristic_relevance, load_quiz
from app.settings import BACKEND_DIR
from app.tokens import TokenCounter
from tests.conftest import FakeJudge, make_answers, make_settings

GOOD = {
    "task_clear": 0.95,
    "first_try_success": 0.95,
    "needs_clarification": 0.05,
    "has_constraints": 0.95,
    "has_output_format": 0.95,
    "has_audience": 0.95,
    "conflicting": 0.02,
}


@pytest.fixture
def client(cfg, tmp_path):
    judge = FakeJudge(
        make_answers(cfg, nouls=GOOD, scores={"specificity": 3, "context_given": 2, "ambiguity": 0})
    )
    # No TypeSafe key in tests: relevance uses the keyword heuristic, so runs are unranked...
    app = create_app(
        make_settings(tmp_path, quiz_rate_limit="100/hour"), judge=judge, counter=TokenCounter(), config=cfg
    )
    with TestClient(app) as c:
        yield c


def answers_for(quiz, *, off_topic=False):
    out = []
    for r in quiz["rounds"]:
        text = (
            "Write a limerick about cats for my kids, under 50 words."
            if off_topic
            else f"{r['scenario']} Please do this well."
        )
        out.append({"id": r["id"], "prompt": text})
    return out


def test_quiz_config_is_valid():
    q = load_quiz(BACKEND_DIR / "config" / "quiz.yaml")
    assert len(q.rounds) == 5 and q.grade(95) == ("A+", "Prompt whisperer") and q.grade(10)[0] == "F"


def test_get_quiz(client):
    q = client.get("/api/quiz").json()
    assert len(q["rounds"]) == 5 and all(r["weak"] and r["scenario"] for r in q["rounds"])


def test_submit_scores_on_the_server(client):
    q = client.get("/api/quiz").json()
    d = client.post("/api/quiz/submit", json={"nickname": "Rohit", "answers": answers_for(q)}).json()
    assert d["score"] >= 90 and d["grade"] in {"A+", "A"} and len(d["rounds"]) == 5
    assert all(r["on_task"] for r in d["rounds"])


def test_off_topic_rewrites_are_capped(client):
    q = client.get("/api/quiz").json()
    d = client.post(
        "/api/quiz/submit", json={"nickname": "Cheater", "answers": answers_for(q, off_topic=True)}
    ).json()
    assert d["score"] <= 20 and not any(r["on_task"] for r in d["rounds"])


def test_unchanged_prompt_scores_zero(client):
    q = client.get("/api/quiz").json()
    ans = answers_for(q)
    ans[0]["prompt"] = q["rounds"][0]["weak"]
    d = client.post("/api/quiz/submit", json={"nickname": "Lazy", "answers": ans}).json()
    assert d["rounds"][0]["score"] == 0


def test_every_round_must_be_answered_and_nickname_checked(client):
    q = client.get("/api/quiz").json()
    r = client.post("/api/quiz/submit", json={"nickname": "Rohit", "answers": answers_for(q)[:3]})
    assert r.status_code == 400
    r = client.post("/api/quiz/submit", json={"nickname": "<script>", "answers": answers_for(q)})
    assert r.status_code == 422


def test_leaderboard_only_ranks_jev_judged_runs(client):
    q = client.get("/api/quiz").json()
    d = client.post("/api/quiz/submit", json={"nickname": "NoJev", "answers": answers_for(q)}).json()
    assert d["ranked"] is False  # no TypeSafe key in tests → heuristic relevance → unranked
    assert client.get("/api/quiz/leaderboard").json()["entries"] == []
    assert client.get("/api/quiz/leaderboard?period=bogus").status_code == 400


def test_heuristic_relevance():
    s = "Client Mr. Tan hasn't paid invoice #1042 for 48,000, now 21 days overdue."
    assert heuristic_relevance(s, "Email Mr. Tan about the overdue invoice #1042") >= 0.2
    assert heuristic_relevance(s, "Write a poem about the ocean") < 0.2


def test_jev_judged_runs_are_ranked_best_per_nickname(cfg, tmp_path, monkeypatch):
    class FakeAnswer:
        noul = 0.9

    class FakeResponse:
        answers = {"on_task": FakeAnswer()}

    class FakeTypeSafe:
        def __init__(self, **kw):
            pass

        async def system_one(self, **kw):
            return FakeResponse()

    monkeypatch.setattr("app.quiz.AsyncTypeSafeClient", FakeTypeSafe)
    judge = FakeJudge(
        make_answers(cfg, nouls=GOOD, scores={"specificity": 3, "context_given": 2, "ambiguity": 0})
    )
    app = create_app(
        make_settings(tmp_path, quiz_rate_limit="100/hour", typesafe_api_key="k"),
        judge=judge,
        counter=TokenCounter(),
        config=cfg,
    )
    with TestClient(app) as c:
        q = c.get("/api/quiz").json()
        first = c.post("/api/quiz/submit", json={"nickname": "Asha", "answers": answers_for(q)}).json()
        assert first["ranked"] is True and first["rank"] == 1 and first["top_score"] is True
        c.post(
            "/api/quiz/submit", json={"nickname": "asha", "answers": answers_for(q)}
        )  # same person, other case
        c.post("/api/quiz/submit", json={"nickname": "Ben", "answers": answers_for(q)})
        board = c.get("/api/quiz/leaderboard").json()["entries"]
        assert [e["nickname"].lower() for e in board] == ["asha", "ben"]  # one row per nickname
        assert c.get("/api/quiz/leaderboard?period=week").json()["entries"]
