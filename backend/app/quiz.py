"""Prompt-engineering quiz (/quiz.html): rewrite five weak prompts, get a grade, climb the leaderboard.

    GET  /api/quiz              the rounds (config/quiz.yaml)
    POST /api/quiz/submit       score every rewrite on the server, store the result
    GET  /api/quiz/leaderboard  best score per nickname (all time or this week)

Scores are computed here, never trusted from the browser. Each rewrite gets the normal Lint Score, and
a second one-question Jev call checks it still asks for that round's task (so pasting one generic
"perfect prompt" into every round doesn't work). Only runs judged by Jev are ranked; if Jev is down the
heuristic scores the run but it's marked unranked.
"""

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from sqlalchemy import select
from typesafe_sdk import AsyncTypeSafeClient, Noul, TypeSafeError

from app.auth import ApiError
from app.db import QuizResult, utcnow
from app.settings import Settings

log = logging.getLogger("promptlint.quiz")
NICK_RE = re.compile(r"^[A-Za-z0-9 _.\-]{2,24}$")
STOPWORDS = set(
    "about after again because before being could doesn each from have into just like make more most only other over same should some such than that their them then there these they this those very want what when where which while with would your you'll you're".split()
)


@dataclass(frozen=True)
class Round:
    id: str
    title: str
    scenario: str
    weak: str


@dataclass(frozen=True)
class QuizConfig:
    version: int
    rounds: tuple[Round, ...]
    grades: tuple[tuple[int, str, str], ...]  # (min, grade, title), highest first

    def grade(self, score: int) -> tuple[str, str]:
        for minimum, grade, title in self.grades:
            if score >= minimum:
                return grade, title
        return self.grades[-1][1], self.grades[-1][2]


def load_quiz(path: Path) -> QuizConfig:
    raw = yaml.safe_load(path.read_text())
    grades = sorted(((int(g["min"]), g["grade"], g["title"]) for g in raw["grades"]), reverse=True)
    return QuizConfig(int(raw.get("version", 1)), tuple(Round(**r) for r in raw["rounds"]), tuple(grades))


class Answer(BaseModel):
    id: str
    prompt: str = Field(..., max_length=4000)


class Submission(BaseModel):
    nickname: str
    answers: list[Answer] = Field(..., min_length=1, max_length=10)

    @field_validator("nickname")
    @classmethod
    def _nick(cls, v: str) -> str:
        v = " ".join(v.split())
        if not NICK_RE.match(v):
            raise ValueError("Nickname: 2–24 letters, numbers, spaces, dots, dashes or underscores.")
        return v


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$.]+", text.lower()) if len(w) >= 4 and w not in STOPWORDS}


def heuristic_relevance(scenario: str, prompt: str) -> float:
    """Share of the scenario's key words that the rewrite uses (fallback when Jev is unavailable)."""
    key = _words(scenario)
    return len(key & _words(prompt)) / len(key) if key else 1.0


def build_quiz_router(limiter: Limiter, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/quiz", tags=["quiz"])
    quiz = load_quiz(settings.config_dir / "quiz.yaml")
    rounds = {r.id: r for r in quiz.rounds}
    jev: dict[str, AsyncTypeSafeClient | None] = {}

    def _client() -> AsyncTypeSafeClient | None:
        if "c" not in jev:
            jev["c"] = (
                AsyncTypeSafeClient(api_key=settings.typesafe_api_key, model=settings.jev_model, timeout=10)
                if settings.typesafe_api_key
                else None
            )
        return jev["c"]

    async def relevance(r: Round, prompt: str) -> tuple[float, bool]:
        """(probability the rewrite asks for this round's task, judged_by_jev)."""
        client = _client()
        if client is not None:
            try:
                resp = await asyncio.wait_for(
                    client.system_one(
                        state={"brief": r.scenario, "prompt": prompt},
                        questions={
                            "on_task": Noul(
                                instructions="Does `prompt` ask an assistant to do the task described in `brief`?",
                                criteria={
                                    "true": "It asks for the same task as the brief (it may add or omit details).",
                                    "false": "It asks for something unrelated to the brief, or for nothing specific.",
                                },
                            )
                        },
                    ),
                    timeout=15,
                )
                return float(resp.answers["on_task"].noul), True
            except (TimeoutError, TypeSafeError, KeyError):
                log.warning("quiz relevance check fell back to the heuristic")
        return (1.0 if heuristic_relevance(r.scenario, prompt) >= 0.2 else 0.0), False

    @router.get("")
    async def get_quiz():
        return {
            "version": quiz.version,
            "rounds": [
                {"id": r.id, "title": r.title, "scenario": r.scenario, "weak": r.weak} for r in quiz.rounds
            ],
            "grades": [{"min": m, "grade": g, "title": t} for m, g, t in quiz.grades],
        }

    async def _leaderboard(request: Request, period: str, limit: int = 20) -> list[dict]:
        q = select(QuizResult).where(QuizResult.ranked.is_(True))
        if period == "week":
            q = q.where(QuizResult.created_at >= utcnow() - dt.timedelta(days=7))
        async with request.app.state.sessionmaker() as s:
            rows = (
                await s.scalars(q.order_by(QuizResult.score.desc(), QuizResult.created_at).limit(500))
            ).all()
        best: dict[str, QuizResult] = {}
        for row in rows:  # best run per nickname (case-insensitive)
            best.setdefault(row.nickname.lower(), row)
        return [
            {
                "rank": i + 1,
                "nickname": r.nickname,
                "score": r.score,
                "grade": r.grade,
                "date": r.created_at.date().isoformat(),
            }
            for i, r in enumerate(list(best.values())[:limit])
        ]

    @router.get("/leaderboard")
    async def leaderboard(request: Request, period: str = "all"):
        if period not in ("all", "week"):
            raise ApiError(400, "invalid_request", "period must be 'all' or 'week'")
        return {"period": period, "entries": await _leaderboard(request, period)}

    @router.post("/submit")
    @limiter.limit(lambda: settings.quiz_rate_limit)
    async def submit(request: Request, body: Submission):
        answered = {a.id: a.prompt.strip() for a in body.answers}
        if set(answered) != set(rounds):
            raise ApiError(400, "invalid_request", "Answer every round exactly once.")
        analyzer = request.app.state.analyzer

        async def score_round(r: Round) -> dict:
            text = answered[r.id]
            if len(text) < 10 or text.lower() == r.weak.lower():
                return {
                    "id": r.id,
                    "title": r.title,
                    "score": 0,
                    "verdict": "likely_to_fail",
                    "on_task": True,
                    "feedback": "Rewrite the prompt: it's empty or unchanged.",
                    "judged_by_jev": True,
                }
            a, (on_task, judged) = await asyncio.gather(
                analyzer.analyze(text, backend="auto"), relevance(r, text)
            )
            score = a.lint.lint_score
            feedback = a.tips[0].text if a.tips else "Nothing important missing. Great rewrite."
            if on_task < 0.5:
                score = min(score, 20)
                feedback = "This doesn't ask for the round's task. Rewrite the prompt you were given, using the scenario's facts."
            return {
                "id": r.id,
                "title": r.title,
                "score": score,
                "verdict": a.lint.verdict,
                "on_task": on_task >= 0.5,
                "feedback": feedback,
                "judged_by_jev": judged and not a.judged.degraded and a.judged.backend == "jev",
            }

        results = await asyncio.gather(*(score_round(r) for r in quiz.rounds))
        total = round(sum(r["score"] for r in results) / len(results))
        grade, title = quiz.grade(total)
        ranked = all(r["judged_by_jev"] for r in results)
        ip = request.client.host if request.client else ""
        row = QuizResult(
            nickname=body.nickname,
            score=total,
            grade=grade,
            rounds=json.dumps([{k: r[k] for k in ("id", "score", "on_task")} for r in results]),
            ranked=ranked,
            ip_hash=hmac.new(settings.prompt_hash_salt.encode(), ip.encode(), hashlib.sha256).hexdigest(),
        )
        async with request.app.state.sessionmaker() as s:
            s.add(row)
            await s.commit()
            better = await s.scalar(
                select(QuizResult.id).where(QuizResult.ranked.is_(True), QuizResult.score > total).limit(1)
            )
        board = await _leaderboard(request, "all", 10)
        rank = next(
            (
                e["rank"]
                for e in board
                if e["nickname"].lower() == body.nickname.lower() and e["score"] == total
            ),
            None,
        )
        log.info("quiz nickname_len=%d score=%d grade=%s ranked=%s", len(body.nickname), total, grade, ranked)
        return {
            "score": total,
            "grade": grade,
            "title": title,
            "ranked": ranked,
            "rank": rank,
            "top_score": better is None,
            "rounds": results,
            "leaderboard": board,
        }

    return router
