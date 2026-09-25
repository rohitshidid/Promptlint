"""Builds reports: one backend judgment + token counting in parallel, then deterministic scoring.

`Analyzer.analyze()` returns an `Analysis`; two renderers turn it into
  - the website report  (`web_report`, PromptLint.md §10)
  - the public API reply (`v1_response`, prompt-quality-scorer.md §11)
The eval scripts use the same path, so they measure exactly what users get.
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from app import cost, scoring, tips
from app.backends import HEURISTIC_VERSION, BackendRouter, Judged
from app.config import AppConfig
from app.schemas import (
    AnalyzeResponse,
    BackendInfo,
    Check,
    CostEstimate,
    Dimension,
    Labelled,
    Meta,
    MissingComponent,
    ModelCost,
    ScoreResponse,
    Signals,
    Specificity,
    Suggestion,
    TaskType,
    TipOut,
    Tokens,
    V1Scores,
    V1Tokens,
)
from app.tokens import TokenCount, TokenCounter, tiktoken_count

log = logging.getLogger("promptlint.analyze")

# (id, label, kind) — kind: "good" passes when high, "bad" passes when low,
# "info*" is shown but never moves the Lint Score (PromptLint.md §7).
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("task_clear", "Task is clear", "good"),
    ("context_given", "Enough context", "good"),
    ("has_goal", "States the goal", "info"),
    ("has_constraints", "Sets constraints", "good"),
    ("has_output_format", "Says the output format", "good"),
    ("has_audience", "Names the audience", "good"),
    ("has_success_criteria", "Says what done looks like", "info"),
    ("has_examples", "Includes an example", "info"),
    ("conflicting", "No conflicting instructions", "bad"),
    ("multi_task", "One task at a time", "bad"),
    ("needs_current_info", "Doesn't need live data", "info_bad"),
)

CHECK_DETAIL = {
    "task_clear": ("States what to do", "Doesn't clearly say what to do"),
    "context_given": ("Gives enough background", "Missing background the model can't guess"),
    "has_goal": ("Says why it's needed", "Doesn't say why or how it'll be used"),
    "has_constraints": ("Sets limits on the answer", "No limits on length, tone, or scope"),
    "has_output_format": ("Says what form the answer takes", "Doesn't say what form the answer takes"),
    "has_audience": ("Says who the answer is for", "Doesn't say who it's for"),
    "has_success_criteria": ("Says what a finished answer includes", "No way to check the answer is done"),
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


def prompt_hash(prompt: str, system: str | None = None) -> str:
    return hashlib.sha256((prompt + "\x00" + (system or "")).encode("utf-8")).hexdigest()[:16]


class _TTLCache:
    """Tiny LRU with expiry for judgments, keyed by prompt hash + backend. Holds no prompt text."""

    def __init__(self, size: int, ttl_s: int):
        self._data: OrderedDict[str, tuple[float, Judged]] = OrderedDict()
        self._size, self._ttl = size, ttl_s

    def get(self, key: str) -> Judged | None:
        item = self._data.get(key)
        if item is None:
            return None
        stamp, value = item
        if time.monotonic() - stamp > self._ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: Judged) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._size:
            self._data.popitem(last=False)


@dataclass(frozen=True)
class CostRow:
    model: object  # ModelPrice
    count: TokenCount
    low: float
    high: float
    p50: float
    p90: float


@dataclass(frozen=True)
class Analysis:
    key: str
    judged: Judged
    cached: bool
    raw: dict[str, float]
    lint: scoring.ScoreResult
    pqs: scoring.PqsResult
    low_confidence: bool
    task_type: str
    output_range: tuple[int, int]
    output_p50: int
    output_p90: int
    costs: list[CostRow]
    tier: str
    suggested_model: str | None
    tips: list[tips.Tip]
    input_o200k: int
    elapsed_ms: int


class Analyzer:
    def __init__(
        self,
        *,
        router: BackendRouter,
        counter: TokenCounter,
        config: AppConfig,
        cache_size: int = 512,
        cache_ttl_s: int = 3600,
    ):
        self.router = router
        self.counter = counter
        self.cfg = config
        self._cache = _TTLCache(cache_size, cache_ttl_s) if cache_size > 0 else None
        self._inflight: dict[str, asyncio.Future[Judged]] = {}

    async def _judge(self, prompt: str, system: str | None, backend: str, key: str) -> tuple[Judged, bool]:
        ckey = f"{key}:{backend}"
        if self._cache is not None and (hit := self._cache.get(ckey)) is not None:
            return hit, True
        # Collapse concurrent identical requests into one backend call.
        if ckey in self._inflight:
            return await asyncio.shield(self._inflight[ckey]), True
        fut: asyncio.Future[Judged] = asyncio.get_running_loop().create_future()
        self._inflight[ckey] = fut
        try:
            judged = await self.router.judge(prompt, system, backend)
            fut.set_result(judged)
            # Don't cache a fallback: the next request should try Jev again.
            if self._cache is not None and not judged.degraded:
                self._cache.put(ckey, judged)
            return judged, False
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved so an unawaited future doesn't warn
            raise
        finally:
            del self._inflight[ckey]

    async def analyze(
        self,
        prompt: str,
        model_ids: list[str] | None = None,
        *,
        system: str | None = None,
        backend: str = "auto",
    ) -> Analysis:
        start = time.perf_counter()
        cfg = self.cfg
        key = prompt_hash(prompt, system)
        ids = model_ids or list(cfg.prices.default_models)
        models = [m for m in (cfg.prices.get(i) for i in ids) if m is not None]
        if not models:
            raise ValueError("None of the requested models are in the price table.")

        (judged, cached), counts = await asyncio.gather(
            self._judge(prompt, system, backend, key),
            asyncio.gather(*(self.counter.count(prompt, m) for m in models)),
        )
        answers = judged.answers
        raw = scoring.raw_values(answers)
        task_type = answers.choices["task_type"].choice
        length_probs = answers.scores["expected_length"].probabilities
        ranges = cfg.prices.output_ranges
        out_low, out_high = cost.output_range(length_probs, ranges)
        p50 = scoring.output_quantile(length_probs, ranges, 0.5)
        p90 = scoring.output_quantile(length_probs, ranges, 0.9)
        rows = []
        for m, c in zip(models, counts, strict=True):
            lo, hi = cost.cost_range(c.tokens, out_low, out_high, m)
            c50, c90 = cost.cost_range(c.tokens, p50, p90, m)
            rows.append(CostRow(m, c, lo, hi, c50, c90))
        tier = scoring.tier_hint(answers.scores["complexity"].score, cfg.weights)
        lint = scoring.lint_score(raw, cfg.weights)
        pqs = scoring.pqs_score(raw, task_type, cfg.pqs)
        analysis = Analysis(
            key=key,
            judged=judged,
            cached=cached,
            raw=raw,
            lint=lint,
            pqs=pqs,
            low_confidence=scoring.low_confidence(answers, cfg.weights),
            task_type=task_type,
            output_range=(out_low, out_high),
            output_p50=p50,
            output_p90=p90,
            costs=rows,
            tier=tier,
            suggested_model=self._suggest(tier),
            tips=tips.select_tips(raw, cfg.tips, cfg.weights),
            input_o200k=tiktoken_count(prompt),
            elapsed_ms=round((time.perf_counter() - start) * 1000),
        )
        # Logs keep a hash, the scores, latency and the backend — never the prompt (PromptLint.md §14).
        log.info(
            "analyze hash=%s lint=%d pqs=%d backend=%s degraded=%s model=%s judge_ms=%d cached=%s",
            key,
            lint.lint_score,
            pqs.pqs_score,
            judged.backend,
            judged.degraded,
            answers.model,
            answers.latency_ms,
            cached,
        )
        return analysis

    def checks(self, raw: dict[str, float]) -> list[Check]:
        out = []
        for cid, label, kind in CHECKS:
            if cid not in raw:
                continue
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

    # ------------------------------------------------------------ renderers
    def web_report(self, a: Analysis) -> AnalyzeResponse:
        answers = a.judged.answers
        first = a.costs[0]
        return AnalyzeResponse(
            lint_score=a.lint.lint_score,
            pqs_score=a.pqs.pqs_score,
            verdict=a.lint.verdict,
            first_try_success=round(a.raw["first_try_success"], 4),
            specificity=Specificity(
                value=round(a.raw["specificity"], 4),
                label=scoring.specificity_label(a.raw["specificity"], self.cfg.weights),
            ),
            checks=self.checks(a.raw),
            dimensions=[Dimension(id=i, label=lbl, value=round(a.raw[i], 4)) for i, lbl in DIMENSIONS],
            task_type=TaskType(**vars(answers.choices["task_type"])),
            tier_hint=a.tier,
            suggested_model=a.suggested_model,
            tokens=Tokens(
                input=first.count.tokens, input_exact=first.count.exact, output_range=a.output_range
            ),
            costs=[
                ModelCost(
                    model=r.model.id,
                    name=r.model.name,
                    provider=r.model.provider,
                    tier=r.model.tier,
                    input_tokens=r.count.tokens,
                    input_exact=r.count.exact,
                    low_usd=r.low,
                    high_usd=r.high,
                    note=r.model.note,
                )
                for r in a.costs
            ],
            tips=[TipOut(id=t.id, signal=t.signal, text=t.text, impact=t.impact) for t in a.tips],
            meta=Meta(
                backend=a.judged.backend,
                degraded=a.judged.degraded,
                jev_model=answers.model,
                latency_ms=answers.latency_ms,
                low_confidence=a.low_confidence,
                cached=a.cached,
                caps_applied=list(a.lint.caps_applied),
                uncapped_score=a.lint.uncapped,
                prices_last_updated=self.cfg.prices.last_updated,
                prompt_hash=a.key,
            ),
        )

    def v1_response(
        self,
        a: Analysis,
        *,
        request_id: str,
        include_suggestions: bool,
        include_confidence: bool,
        stored: bool,
    ) -> ScoreResponse:
        answers = a.judged.answers
        cfg = self.cfg
        cx = answers.scores["complexity"]
        cx_probs = {scoring.COMPLEXITY_LABELS[i]: round(p, 4) for i, p in enumerate(cx.probabilities)}
        cx_label = max(cx_probs, key=cx_probs.get)
        tt = answers.choices["task_type"]
        missing = sorted(
            (c for c, p in a.pqs.missing.items() if p >= cfg.pqs.missing_threshold),
            key=lambda c: -a.pqs.missing[c],
        )
        confidence = None
        if include_confidence:
            confidence = {name: round(s.confidence, 4) for name, s in answers.scores.items()}
            confidence["task_type"] = round(tt.confidence, 4)
            # A yes/no answer is as confident as it is far from 0.5.
            confidence.update({n: round(abs(p - 0.5) * 2, 4) for n, p in answers.nouls.items()})
        version = answers.model if a.judged.backend == "jev" else HEURISTIC_VERSION
        return ScoreResponse(
            id=request_id.replace("req_", "scr_", 1),
            overall_score=a.lint.lint_score,
            scores=V1Scores(lint_score=a.lint.lint_score, pqs_score=a.pqs.pqs_score, verdict=a.lint.verdict),
            signals=Signals(
                clarity=round(a.pqs.clarity, 4),
                specificity=round(a.pqs.specificity, 4),
                completeness=round(a.pqs.completeness, 4),
                reiteration_risk=round(a.pqs.reiteration_risk, 4),
                first_try_success=round(a.raw["first_try_success"], 4),
                task_type=Labelled(
                    label=tt.choice,
                    confidence=round(tt.confidence, 4),
                    probs={k: round(v, 4) for k, v in tt.probabilities.items()}
                    if include_confidence
                    else None,
                ),
                complexity=Labelled(
                    label=cx_label,
                    confidence=round(cx.confidence, 4),
                    probs=cx_probs if include_confidence else None,
                ),
                missing_components=missing,
                missing_detail=[
                    MissingComponent(component=c, probability=round(p, 4))
                    for c, p in sorted(a.pqs.missing.items(), key=lambda kv: -kv[1])
                ]
                if include_confidence
                else None,
            ),
            checks=self.checks(a.raw),
            tokens=V1Tokens(
                input={"o200k_base": a.input_o200k, **{r.model.id: r.count.tokens for r in a.costs}},
                input_exact={"o200k_base": True, **{r.model.id: r.count.exact for r in a.costs}},
                output_p50=a.output_p50,
                output_p90=a.output_p90,
                output_range=a.output_range,
            ),
            cost_estimates=[
                CostEstimate(
                    model=r.model.id,
                    name=r.model.name,
                    provider=r.model.provider,
                    input_tokens=r.count.tokens,
                    input_exact=r.count.exact,
                    usd_p50=r.p50,
                    usd_p90=r.p90,
                    usd_low=r.low,
                    usd_high=r.high,
                    pricing_verified=cfg.prices.last_updated,
                    note=r.model.note,
                )
                for r in a.costs
            ],
            suggestions=[Suggestion(id=t.id, text=t.text, impact=t.impact) for t in a.tips]
            if include_suggestions
            else None,
            confidence=confidence,
            tier_hint=a.tier,
            suggested_model=a.suggested_model,
            backend=BackendInfo(
                name=a.judged.backend,
                version=version,
                degraded=a.judged.degraded,
                fallback_reason=a.judged.fallback_reason,
            ),
            stored=stored,
            cached=a.cached,
            latency_ms=a.elapsed_ms,
        )
