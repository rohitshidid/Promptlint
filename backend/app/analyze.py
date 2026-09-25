"""Builds the full report: one Jev call + token counting in parallel, then deterministic scoring.

Used by the API and by the eval scripts, so both measure exactly the same code path.
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

from app import cost, scoring, tips
from app.config import AppConfig
from app.jev_client import JevAnswers, Judge
from app.schemas import (
    AnalyzeResponse,
    Check,
    Dimension,
    Meta,
    ModelCost,
    Specificity,
    TaskType,
    TipOut,
    Tokens,
)
from app.tokens import TokenCounter

log = logging.getLogger("promptlint.analyze")

# (id, label, kind) — kind: "good" passes when high, "bad" passes when low,
# "info" is shown but never scored (PromptLint.md §7).
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("task_clear", "Task is clear", "good"),
    ("context_given", "Enough context", "good"),
    ("has_constraints", "Sets constraints", "good"),
    ("has_output_format", "Says the output format", "good"),
    ("has_audience", "Names the audience", "good"),
    ("has_examples", "Includes an example", "info"),
    ("conflicting", "No conflicting instructions", "bad"),
    ("multi_task", "One task at a time", "bad"),
    ("needs_current_info", "Doesn't need live data", "info_bad"),
)

CHECK_DETAIL = {
    "task_clear": ("States what to do", "Doesn't clearly say what to do"),
    "context_given": ("Gives enough background", "Missing background the model can't guess"),
    "has_constraints": ("Sets limits on the answer", "No limits on length, tone, or scope"),
    "has_output_format": ("Says what form the answer takes", "Doesn't say what form the answer takes"),
    "has_audience": ("Says who the answer is for", "Doesn't say who it's for"),
    "has_examples": ("Includes an example", "No example (optional)"),
    "conflicting": ("Instructions are consistent", "Some instructions contradict each other"),
    "multi_task": ("Asks for one deliverable", "Asks for several deliverables"),
    "needs_current_info": ("Doesn't depend on recent info", "Depends on recent or live info"),
}

DIMENSIONS = (
    ("task_clear", "Clarity"),
    ("specificity", "Specificity"),
    ("context_given", "Context"),
    ("has_output_format", "Format"),
    ("has_constraints", "Constraints"),
)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


class _TTLCache:
    """Tiny LRU with expiry for Jev answers, keyed by prompt hash. Holds no prompt text."""

    def __init__(self, size: int, ttl_s: int):
        self._data: OrderedDict[str, tuple[float, JevAnswers]] = OrderedDict()
        self._size, self._ttl = size, ttl_s

    def get(self, key: str) -> JevAnswers | None:
        item = self._data.get(key)
        if item is None:
            return None
        stamp, value = item
        if time.monotonic() - stamp > self._ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: JevAnswers) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._size:
            self._data.popitem(last=False)


class Analyzer:
    def __init__(
        self,
        *,
        judge: Judge,
        counter: TokenCounter,
        config: AppConfig,
        cache_size: int = 512,
        cache_ttl_s: int = 3600,
    ):
        self.judge = judge
        self.counter = counter
        self.cfg = config
        self._cache = _TTLCache(cache_size, cache_ttl_s) if cache_size > 0 else None
        self._inflight: dict[str, asyncio.Future[JevAnswers]] = {}

    async def _answers(self, prompt: str, key: str) -> tuple[JevAnswers, bool]:
        if self._cache is not None and (hit := self._cache.get(key)) is not None:
            return hit, True
        # Collapse concurrent identical requests into one Jev call.
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key]), True
        fut: asyncio.Future[JevAnswers] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            answers = await self.judge.judge(prompt)
            fut.set_result(answers)
            if self._cache is not None:
                self._cache.put(key, answers)
            return answers, False
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved so an unawaited future doesn't warn
            raise
        finally:
            del self._inflight[key]

    async def analyze(self, prompt: str, model_ids: list[str] | None = None) -> AnalyzeResponse:
        cfg = self.cfg
        key = prompt_hash(prompt)
        ids = model_ids or list(cfg.prices.default_models)
        models = [m for m in (cfg.prices.get(i) for i in ids) if m is not None]
        if not models:
            raise ValueError("None of the requested models are in the price table.")

        (answers, cached), counts = await asyncio.gather(
            self._answers(prompt, key),
            asyncio.gather(*(self.counter.count(prompt, m) for m in models)),
        )

        raw = scoring.raw_values(answers)
        score = scoring.lint_score(raw, cfg.weights)
        low_conf = scoring.low_confidence(answers, cfg.weights)

        out_low, out_high = cost.output_range(
            answers.scores["expected_length"].probabilities, cfg.prices.output_ranges
        )
        costs = []
        for m, c in zip(models, counts, strict=True):
            lo, hi = cost.cost_range(c.tokens, out_low, out_high, m)
            costs.append(
                ModelCost(
                    model=m.id,
                    name=m.name,
                    provider=m.provider,
                    tier=m.tier,
                    input_tokens=c.tokens,
                    input_exact=c.exact,
                    low_usd=lo,
                    high_usd=hi,
                    note=m.note,
                )
            )

        tier = scoring.tier_hint(answers.scores["complexity"].score, cfg.weights)
        spec = raw["specificity"]
        first = costs[0]
        report = AnalyzeResponse(
            lint_score=score.lint_score,
            verdict=score.verdict,
            first_try_success=round(raw["first_try_success"], 4),
            specificity=Specificity(value=round(spec, 4), label=scoring.specificity_label(spec, cfg.weights)),
            checks=self._checks(raw),
            dimensions=[Dimension(id=i, label=lbl, value=round(raw[i], 4)) for i, lbl in DIMENSIONS],
            task_type=TaskType(**vars(answers.choices["task_type"])),
            tier_hint=tier,
            suggested_model=self._suggest(tier),
            tokens=Tokens(
                input=first.input_tokens, input_exact=first.input_exact, output_range=(out_low, out_high)
            ),
            costs=costs,
            tips=[TipOut(**vars(t)) for t in tips.select_tips(raw, cfg.tips, cfg.weights)],
            meta=Meta(
                jev_model=answers.model,
                latency_ms=answers.latency_ms,
                low_confidence=low_conf,
                cached=cached,
                caps_applied=list(score.caps_applied),
                uncapped_score=score.uncapped,
                prices_last_updated=cfg.prices.last_updated,
                prompt_hash=key,
            ),
        )
        # Logs keep a hash, the scores, latency and the Jev model — never the prompt (PromptLint.md §14).
        log.info(
            "analyze hash=%s score=%d verdict=%s first_try=%.2f jev_model=%s jev_ms=%d cached=%s",
            key,
            score.lint_score,
            score.verdict,
            raw["first_try_success"],
            answers.model,
            answers.latency_ms,
            cached,
        )
        return report

    def _checks(self, raw: dict[str, float]) -> list[Check]:
        out = []
        for cid, label, kind in CHECKS:
            p = raw[cid]
            good_when_high = kind in ("good", "info")
            passed = p >= 0.5 if good_when_high else p < 0.5
            ok_text, bad_text = CHECK_DETAIL[cid]
            out.append(
                Check(
                    id=cid,
                    label=label,
                    passed=passed,
                    probability=round(p, 4),
                    informational=kind.startswith("info"),
                    detail=ok_text if passed else bad_text,
                )
            )
        return out

    def _suggest(self, tier: str) -> str | None:
        """Cheapest model in the price table at the hinted tier (by output price)."""
        candidates = [m for m in self.cfg.prices.models if m.tier == tier]
        if not candidates:
            return None
        return min(candidates, key=lambda m: (m.output, m.input)).id
