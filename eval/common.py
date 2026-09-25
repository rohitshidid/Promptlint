"""Shared eval plumbing: run prompts through the production Jev judge, cache raw answers, rescore offline.

The cache key includes the Jev model and a hash of questions.yaml, so changing either triggers fresh
calls, while changing weights.yaml or tips.yaml only rescores (no API spend). That is the tuning loop
from PromptLint.md §12: adjust → re-run → commit only if metrics improve.
"""

import asyncio
import hashlib
import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import scoring  # noqa: E402
from app.config import AppConfig, load_config  # noqa: E402
from app.jev_client import ChoiceResult, JevAnswers, JevError, JevJudge, ScoreResult  # noqa: E402
from app.settings import Settings  # noqa: E402

EVAL = ROOT / "eval"
DATA = EVAL / "data"
RESULTS = EVAL / "results"
CACHE = RESULTS / "cache"
SUMMARY = RESULTS / "summary.json"
PUBLIC_SUMMARY = ROOT / "frontend" / "assets" / "eval-summary.json"


def load_jsonl(name: str) -> list[dict]:
    with (DATA / name).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def settings() -> Settings:
    return Settings()


def config() -> AppConfig:
    return load_config(BACKEND / "config")


def _questions_hash() -> str:
    return hashlib.sha256((BACKEND / "config" / "questions.yaml").read_bytes()).hexdigest()[:10]


def _key(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}|{_questions_hash()}|{prompt}".encode()).hexdigest()[:24]


def _to_json(a: JevAnswers) -> dict:
    d = asdict(a)
    d.pop("raw", None)
    return d


def _from_json(d: dict) -> JevAnswers:
    return JevAnswers(
        nouls=d["nouls"],
        scores={
            k: ScoreResult(v["score"], v["confidence"], tuple(v["probabilities"]), v["top"])
            for k, v in d["scores"].items()
        },
        choices={k: ChoiceResult(**v) for k, v in d["choices"].items()},
        model=d["model"],
        latency_ms=d["latency_ms"],
        input_tokens=d.get("input_tokens"),
        output_tokens=d.get("output_tokens"),
    )


async def judge_all(
    prompts: list[str], *, concurrency: int = 8, label: str = ""
) -> tuple[dict[str, JevAnswers], list[int]]:
    """Answers for every prompt, from cache when possible. Returns (answers by prompt, fresh latencies)."""
    s = settings()
    cfg = config()
    CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, JevAnswers] = {}
    todo: list[str] = []
    for p in dict.fromkeys(prompts):
        f = CACHE / f"{_key(p, s.jev_model)}.json"
        if f.exists():
            out[p] = _from_json(json.loads(f.read_text()))
        else:
            todo.append(p)

    latencies: list[int] = []
    if todo:
        if not s.typesafe_api_key:
            sys.exit("TYPESAFE_API_KEY is not set (put it in the repo's .env).")
        # Generous deadline for batch runs; the product uses 6 s.
        judge = JevJudge(
            api_key=s.typesafe_api_key,
            model=s.jev_model,
            questions=cfg.questions,
            timeout_s=10,
            deadline_s=60,
            max_retries=4,
        )
        sem = asyncio.Semaphore(concurrency)
        done = 0
        start = time.perf_counter()

        async def one(p: str) -> None:
            nonlocal done
            async with sem:
                for attempt in range(3):
                    try:
                        a = await judge.judge(p)
                        break
                    except JevError as e:
                        if attempt == 2:
                            raise
                        print(f"  retrying after {e.status}: {e.message}", file=sys.stderr)
                        await asyncio.sleep(2 * (attempt + 1))
            out[p] = a
            latencies.append(a.latency_ms)
            (CACHE / f"{_key(p, s.jev_model)}.json").write_text(json.dumps(_to_json(a)))
            done += 1
            if done % 25 == 0 or done == len(todo):
                print(
                    f"  {label} {done}/{len(todo)} Jev calls ({time.perf_counter() - start:.0f}s)",
                    file=sys.stderr,
                )

        print(f"{label}: {len(out)} cached, {len(todo)} to call Jev for", file=sys.stderr)
        try:
            await asyncio.gather(*(one(p) for p in todo))
        finally:
            await judge.aclose()
    else:
        print(f"{label}: all {len(out)} answers cached", file=sys.stderr)
    return out, latencies


def lint(answers: JevAnswers, cfg: AppConfig) -> scoring.ScoreResult:
    return scoring.lint_score(scoring.raw_values(answers), cfg.weights)


def pqs(answers: JevAnswers, cfg: AppConfig) -> int:
    return scoring.pqs_score(
        scoring.raw_values(answers), answers.choices["task_type"].choice, cfg.pqs
    ).pqs_score


def score_both(answers: JevAnswers, cfg: AppConfig) -> dict[str, int]:
    return {"lint": lint(answers, cfg).lint_score, "pqs": pqs(answers, cfg)}


async def heuristic_all(prompts: list[str]) -> dict[str, JevAnswers]:
    """The rule-based baseline backend (prompt-quality-scorer.md §10.2): no network, no cache needed."""
    from app.backends import HeuristicJudge

    h = HeuristicJudge(config().questions)
    return {p: await h.judge(p) for p in dict.fromkeys(prompts)}


async def answers_for(
    backend: str, prompts: list[str], label: str
) -> tuple[dict[str, JevAnswers], list[int]]:
    if backend == "heuristic":
        return await heuristic_all(prompts), []
    return await judge_all(prompts, label=label)


def auroc(scores: list[float], labels: list[int]) -> float:
    """Mann–Whitney AUROC with tie handling; no sklearn dependency."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    neg = [s for s, y in zip(scores, labels, strict=True) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def brier(probs: list[float], labels: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, labels, strict=True)) / len(probs)


def reliability(probs: list[float], labels: list[int], bins: int = 5) -> list[dict]:
    rows = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [j for j, p in enumerate(probs) if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if idx:
            rows.append(
                {
                    "bin": f"{lo:.1f}–{hi:.1f}",
                    "n": len(idx),
                    "mean_predicted": sum(probs[j] for j in idx) / len(idx),
                    "observed_rate": sum(labels[j] for j in idx) / len(idx),
                }
            )
    return rows


def latency_stats(ms: list[int]) -> dict | None:
    if not ms:
        return None
    s = sorted(ms)
    return {"median_ms": statistics.median(s), "p95_ms": s[min(len(s) - 1, int(0.95 * len(s)))], "n": len(s)}


def write_result(name: str, data: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def update_summary(section: str, value: dict | None, *, latencies: list[int] | None = None) -> None:
    """Merge one eval's headline numbers into summary.json and publish it for the landing page."""
    summary = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    summary[section] = value
    if latencies:
        prev = summary.get("_latency_samples", [])
        summary["_latency_samples"] = (prev + latencies)[-2000:]
    summary["latency"] = latency_stats(summary.get("_latency_samples", []))
    summary["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    summary["jev_model"] = settings().jev_model
    summary.setdefault(
        "note",
        "Prompt pairs and checklist labels were drafted with an AI assistant for this project; "
        "spot-check them before citing these numbers.",
    )
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    public = {k: v for k, v in summary.items() if not k.startswith("_")}
    PUBLIC_SUMMARY.write_text(json.dumps(public, indent=2) + "\n")
