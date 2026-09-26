"""Public API, /v1 (prompt-quality-scorer.md §11), plus self-serve accounts and key management.

POST   /v1/score            score one prompt                          API key
POST   /v1/score/batch      score up to 50 prompts (plan limit)       API key
POST   /v1/route            score + route, and optionally call the    API key
                            recommended model with the user's keys
GET/POST/DELETE /v1/providers  saved LLM provider keys (encrypted)    session
GET    /v1/pricing          price table + last-verified date          public
GET    /v1/usage            usage by day                              API key or session
GET    /v1/health           liveness + backend and model versions     public
POST   /v1/account/signup | /login | /logout, GET /me, DELETE         session
GET    /v1/keys, POST /v1/keys, DELETE /v1/keys/{id}                  session
"""

import asyncio
import datetime as dt
import re

from fastapi import APIRouter, Depends, Request, Response
from limits import parse
from pydantic import BaseModel, Field
from slowapi import Limiter
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import router as model_router
from app.analyze import Analysis
from app.auth import (
    SESSION_COOKIE,
    ApiError,
    KeyContext,
    check_csrf,
    cookie_secure,
    current_user,
    get_db,
    require_api_key,
    require_user,
    verify_turnstile,
)
from app.backends import HEURISTIC_VERSION
from app.db import (
    ApiKey,
    ProviderKey,
    ScoreEvent,
    Session,
    StoredPrompt,
    UsageDaily,
    User,
    prompts_used,
    record_usage,
    utcnow,
)
from app.jev_client import JevError
from app.providers import ProviderError, check_base_url, run_with_fallback
from app.schemas import (
    Attempt,
    BatchRequest,
    BatchResponse,
    BatchResult,
    Execution,
    NotConnected,
    ProviderKeyIn,
    ProviderKeyUpdate,
    RoutedModel,
    RouteRequest,
    RouteResponse,
    Routing,
    RoutingOptions,
    ScoreRequest,
    ScoreResponse,
)
from app.security import (
    DUMMY_PASSWORD_HASH,
    KeyVault,
    hash_password,
    key_hint,
    mask_key,
    new_api_key,
    new_session_token,
    prompt_digest,
    sha256_hex,
    verify_password,
)
from app.settings import Settings

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def build_v1_router(limiter: Limiter, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["v1"])

    # ------------------------------------------------------------ helpers
    def _rid(request: Request) -> str:
        return request.state.request_id

    def _rate_limit(request: Request, response: Response, ctx: KeyContext) -> None:
        """Per-key requests/minute from the plan, with the X-RateLimit-* headers on every reply."""
        item = parse(f"{ctx.plan.rpm}/minute")
        key = f"v1:key:{ctx.key.id}"
        allowed = request.app.state.key_limiter.hit(item, key)
        stats = request.app.state.key_limiter.get_window_stats(item, key)
        headers = {
            "X-RateLimit-Limit": str(ctx.plan.rpm),
            "X-RateLimit-Remaining": str(max(0, stats.remaining)),
            "X-RateLimit-Reset": str(int(stats.reset_time)),
        }
        response.headers.update(headers)
        if not allowed:
            retry = max(1, int(stats.reset_time - dt.datetime.now(dt.UTC).timestamp()))
            raise ApiError(
                429,
                "rate_limit_exceeded",
                f"Your plan allows {ctx.plan.rpm} requests per minute. Retry in {retry}s."
                " PromptLint is free; we're raising limits on a rolling basis as capacity grows.",
                {**headers, "Retry-After": str(retry)},
            )

    async def _quota(db: AsyncSession, response: Response, ctx: KeyContext, prompts: int) -> None:
        """Account-wide daily/monthly prompt quotas, counted in the database so restarts don't reset them."""
        today = utcnow().date()
        for period, limit, since in (
            ("day", ctx.plan.daily, today),
            ("month", ctx.plan.monthly, today.replace(day=1)),
        ):
            if limit is None:
                continue
            used = await prompts_used(db, ctx.user.id, since)
            response.headers["X-Quota-Limit"] = str(limit)
            response.headers["X-Quota-Remaining"] = str(max(0, limit - used - prompts))
            response.headers["X-Quota-Period"] = period
            if used + prompts > limit:
                raise ApiError(
                    429,
                    "quota_exceeded",
                    f"This request needs {prompts} prompt(s) but only {max(0, limit - used)} of your "
                    f"{limit}/{period} remain on the {ctx.plan.name} plan. PromptLint is free; we're raising "
                    "limits on a rolling basis as capacity grows.",
                )

    def _check_input(request: Request, prompt: str, system: str | None, models: list[str] | None) -> None:
        s = request.app.state.settings
        if len(prompt) > s.v1_max_prompt_chars:
            raise ApiError(
                413,
                "prompt_too_long",
                f"prompt is {len(prompt):,} characters; the limit is {s.v1_max_prompt_chars:,}.",
            )
        if system and len(system) > s.v1_max_system_chars:
            raise ApiError(
                413,
                "prompt_too_long",
                f"system is {len(system):,} characters; the limit is {s.v1_max_system_chars:,}.",
            )
        unknown = [m for m in models or [] if request.app.state.config.prices.get(m) is None]
        if unknown:
            raise ApiError(400, "unknown_model", f"Unknown model {unknown[0]!r}. See GET /v1/pricing.")

    async def _analyze(request: Request, prompt: str, system: str | None, models, backend: str) -> Analysis:
        analyzer = request.app.state.analyzer
        try:
            return await analyzer.analyze(prompt, models, system=system, backend=backend)
        except JevError as e:
            # Only reachable with backend="jev": auto falls back to the heuristic instead.
            raise ApiError(503, "backend_unavailable", e.message) from e

    async def _record(
        db: AsyncSession,
        request: Request,
        ctx: KeyContext,
        a: Analysis,
        prompt: str,
        system,
        store: bool,
        decision: model_router.RouteDecision | None = None,
        executed: dict | None = None,
        used: model_router.RouteDecision | None = None,
    ) -> None:
        s = request.app.state.settings
        event = ScoreEvent(
            request_id=_rid(request),
            key_id=ctx.key.id,
            prompt_hash=prompt_digest(prompt, system, s.prompt_hash_salt),
            prompt_chars=len(prompt),
            backend=a.judged.backend,
            degraded=a.judged.degraded,
            lint_score=a.lint.lint_score,
            pqs_score=a.pqs.pqs_score,
            latency_ms=a.elapsed_ms,
            **_routing_fields(decision, used),
            **(executed or {}),
        )
        db.add(event)
        if store:
            await db.flush()
            db.add(StoredPrompt(event_id=event.id, prompt=prompt, system=system))

    # ------------------------------------------------------------ routing helpers
    def _routing_fields(
        d: model_router.RouteDecision | None, used: model_router.RouteDecision | None = None
    ) -> dict:
        """What the router picked, for the per-key routing stats on the account page.

        `d` is the best fit over every candidate; `used` is the decision among the models you have keys
        for (when that differs). Expected cost and baseline come from the model that would really be used.
        """
        if d is None or d.chosen is None:
            return {}
        u = used if used is not None and used.chosen is not None else d
        c = d.chosen.candidate
        return {
            "strategy": d.strategy,
            "task_type": d.task_type,
            "routed_model": c.id,
            "routed_provider": c.provider,
            "connected_model": u.chosen.candidate.id if u.chosen.candidate.id != c.id else None,
            "baseline_model": u.baseline.candidate.id if u.baseline else None,
            "est_cost_usd": u.chosen.cost_p50,
            "est_baseline_usd": u.baseline.cost_p50 if u.baseline else None,
            "clarify_first": d.action == "clarify_first",
        }

    def _connected_ids(candidates, saved: list[ProviderKey], provider_keys: dict | None = None) -> set[str]:
        """Candidates you could actually call: a saved or per-request key, or your own endpoint."""
        adapters = {r.provider for r in saved if r.provider != "openai_compatible" and r.encrypted_key}
        adapters |= set(provider_keys or {})
        return {
            c.id
            for c in candidates
            if c.inline_key
            or c.source == "connected"
            or (c.adapter == "openai_compatible" and c.source == "custom" and c.base_url)
            or c.adapter in adapters
        }

    def _not_connected_note(pick: RoutedModel, instead: RoutedModel | None, used: bool) -> str:
        who = "your own endpoint" if pick.provider == "Custom endpoint" else pick.provider
        if instead is None:
            return f"{pick.name} is the best fit for this prompt, but you haven't connected {who}."
        verb = "was used instead" if used else "would be used instead"
        return (
            f"{pick.name} is the best fit for this prompt, but you haven't connected {who}, "
            f"so {instead.name} (the best of your connected models) {verb}."
        )

    def _mark_connected(routing: Routing, conn: set[str]) -> None:
        for m in (routing.recommended, routing.fallback, routing.baseline, *routing.alternatives):
            if m is not None:
                m.connected = m.id in conn

    def _with_connections(request: Request, a: Analysis, opts: RoutingOptions, candidates, decision, conn):
        """The routing for the response, plus the decision among connected models (None if not needed)."""
        analyzer = request.app.state.analyzer
        routing = analyzer.routing_out(decision)
        if not conn:
            return routing, None
        _mark_connected(routing, conn)
        if decision.chosen is None or decision.chosen.candidate.id in conn:
            return routing, None
        used = _decide(request, a, opts, [c for c in candidates if c.id in conn])
        instead = analyzer._routed(used.chosen)
        if instead is not None:
            instead.connected = True
        routing.not_connected = NotConnected(
            model=routing.recommended,
            instead=instead,
            note=_not_connected_note(routing.recommended, instead, used=False),
        )
        return routing, used

    def _vault(request: Request) -> KeyVault | None:
        secret = request.app.state.settings.effective_provider_key_secret
        return KeyVault(secret) if secret else None

    async def _saved_providers(db: AsyncSession, user_id: int) -> list[ProviderKey]:
        return list(
            (
                await db.scalars(
                    select(ProviderKey).where(ProviderKey.user_id == user_id).order_by(ProviderKey.id)
                )
            ).all()
        )

    def _connected_candidates(rows: list[ProviderKey]) -> list[model_router.Candidate]:
        """Custom endpoints saved on the account become routing candidates."""
        out = []
        for r in rows:
            if r.provider == "openai_compatible" and r.model and r.tier and r.input_price is not None:
                out.append(
                    model_router.Candidate(
                        id=r.model,
                        name=r.label,
                        provider="Custom endpoint",
                        adapter="openai_compatible",
                        tier=r.tier,
                        input=r.input_price,
                        output=r.output_price or 0.0,
                        source="connected",
                        base_url=r.base_url,
                        api_model=r.model,
                        quality=r.quality,
                    )
                )
        return out

    def _decide(request: Request, a: Analysis, opts: RoutingOptions, candidates):
        analyzer = request.app.state.analyzer
        try:
            return analyzer.decide(a, opts, candidates)
        except ValueError as e:
            raise ApiError(400, "invalid_routing", str(e)) from e

    def _candidates(request: Request, opts: RoutingOptions, connected) -> list[model_router.Candidate]:
        try:
            return request.app.state.analyzer.resolve_candidates(opts, connected)
        except ValueError as e:
            raise ApiError(400, "unknown_model", str(e)) from e

    # ------------------------------------------------------------ scoring
    @router.post("/score", response_model=ScoreResponse, response_model_exclude_none=True)
    async def score(
        request: Request,
        response: Response,
        body: ScoreRequest,
        ctx: KeyContext = Depends(require_api_key),
        db: AsyncSession = Depends(get_db),
    ):
        _rate_limit(request, response, ctx)
        _check_input(request, body.prompt, body.system, body.models)
        saved = await _saved_providers(db, ctx.user.id)
        candidates = _candidates(request, body.routing, _connected_candidates(saved))
        conn = _connected_ids(candidates, saved)
        await _quota(db, response, ctx, 1)
        a = await _analyze(request, body.prompt, body.system, body.models, body.backend)
        decision = _decide(request, a, body.routing, candidates)
        routing, used = _with_connections(request, a, body.routing, candidates, decision, conn)
        await _record(db, request, ctx, a, body.prompt, body.system, body.options.store, decision, used=used)
        await record_usage(
            db, key_id=ctx.key.id, user_id=ctx.user.id, prompts=1, degraded=int(a.judged.degraded)
        )
        await db.commit()
        return request.app.state.analyzer.v1_response(
            a,
            request_id=_rid(request),
            include_suggestions=body.options.include_suggestions,
            include_confidence=body.options.include_confidence,
            stored=body.options.store,
            routing=routing,
        )

    @router.post("/score/batch", response_model=BatchResponse, response_model_exclude_none=True)
    async def score_batch(
        request: Request,
        response: Response,
        body: BatchRequest,
        ctx: KeyContext = Depends(require_api_key),
        db: AsyncSession = Depends(get_db),
    ):
        if len(body.items) > ctx.plan.batch_max:
            raise ApiError(
                400,
                "batch_too_large",
                f"The {ctx.plan.name} plan allows {ctx.plan.batch_max} prompts per batch.",
            )
        _rate_limit(request, response, ctx)
        _check_input(request, "", None, body.models)
        saved = await _saved_providers(db, ctx.user.id)
        candidates = _candidates(request, body.routing, _connected_candidates(saved))
        conn = _connected_ids(candidates, saved)
        await _quota(db, response, ctx, len(body.items))
        sem = asyncio.Semaphore(8)
        rid = _rid(request)

        async def one(i: int, item) -> tuple[int, Analysis | None, dict | None]:
            try:
                if not item.prompt.strip():
                    raise ApiError(400, "invalid_request", "prompt must not be empty")
                _check_input(request, item.prompt, item.system, None)
                async with sem:
                    return (
                        i,
                        await _analyze(request, item.prompt, item.system, body.models, body.backend),
                        None,
                    )
            except ApiError as e:
                return i, None, {"type": e.type, "message": e.message}

        outcomes = await asyncio.gather(*(one(i, it) for i, it in enumerate(body.items)))
        results, scored, degraded = [], 0, 0
        for i, a, err in outcomes:
            item = body.items[i]
            if a is None:
                results.append(BatchResult(index=i, id=item.id, error=err))
                continue
            scored += 1
            degraded += int(a.judged.degraded)
            decision = _decide(request, a, body.routing, candidates)
            routing, used = _with_connections(request, a, body.routing, candidates, decision, conn)
            await _record(
                db, request, ctx, a, item.prompt, item.system, body.options.store, decision, used=used
            )
            results.append(
                BatchResult(
                    index=i,
                    id=item.id,
                    result=request.app.state.analyzer.v1_response(
                        a,
                        request_id=f"{rid}_{i}",
                        include_suggestions=body.options.include_suggestions,
                        include_confidence=body.options.include_confidence,
                        stored=body.options.store,
                        routing=routing,
                    ),
                )
            )
        if scored:
            await record_usage(db, key_id=ctx.key.id, user_id=ctx.user.id, prompts=scored, degraded=degraded)
        await db.commit()
        return BatchResponse(
            id=rid.replace("req_", "bat_", 1), results=results, scored=scored, failed=len(results) - scored
        )

    # ------------------------------------------------------------ routing pipeline
    @router.post("/route", response_model=RouteResponse, response_model_exclude_none=True)
    async def route_prompt(
        request: Request,
        response: Response,
        body: RouteRequest,
        ctx: KeyContext = Depends(require_api_key),
        db: AsyncSession = Depends(get_db),
    ):
        """Score the prompt, pick a model, and (if you have a key for it) call that model.

        With execute=false, no usable key, or a prompt that should be clarified first, you get the
        recommendation only: the same JSON as /v1/score plus `execution.executed = false` and why.
        """
        s = request.app.state.settings
        _rate_limit(request, response, ctx)
        _check_input(request, body.prompt, body.system, body.models)
        saved = await _saved_providers(db, ctx.user.id)
        connected = _connected_candidates(saved)
        candidates = _candidates(request, body.routing, connected)
        conn = _connected_ids(candidates, saved, body.provider_keys)
        await _quota(db, response, ctx, 1)
        a = await _analyze(request, body.prompt, body.system, body.models, body.backend)
        best = _decide(request, a, body.routing, candidates)  # best fit over every candidate
        routing, used = _with_connections(request, a, body.routing, candidates, best, conn)
        decision = used or best  # what would really be called

        execution = Execution(executed=False)
        called = False  # did we actually try any model?
        if not body.execute:
            execution.reason = "execute is false: recommendation only."
        elif decision.action == "clarify_first" and not body.send_anyway:
            execution.reason = (
                "Not sent: the prompt should be clarified first (routing.clarify_reason). "
                "Pass send_anyway: true to call a model regardless."
            )
        else:
            # Keys: per-request first, then saved ones (built-in providers by adapter, endpoints by model).
            vault = _vault(request)
            saved_by_adapter: dict[str, ProviderKey] = {}
            saved_by_model: dict[str, ProviderKey] = {}
            for row in saved:
                if row.provider == "openai_compatible":
                    saved_by_model[row.model or ""] = row
                else:
                    saved_by_adapter[row.provider] = row
            used_rows: dict[str, ProviderKey] = {}

            def key_for(c: model_router.Candidate) -> tuple[bool, str | None]:
                if c.inline_key:
                    return True, c.inline_key
                if body.provider_keys and c.adapter in body.provider_keys:
                    return True, body.provider_keys[c.adapter]
                row = (
                    saved_by_model.get(c.api_model or c.id)
                    if c.adapter == "openai_compatible"
                    else saved_by_adapter.get(c.adapter)
                )
                if row is not None:
                    if row.encrypted_key is None:
                        used_rows[c.id] = row
                        return True, None  # keyless endpoint (e.g. Ollama)
                    if vault is not None:
                        try:
                            used_rows[c.id] = row
                            return True, vault.decrypt(row.encrypted_key)
                        except Exception:  # noqa: BLE001 - wrong/rotated secret: treat as no key
                            return False, None
                # A custom endpoint given inline with no key may not need one (Ollama).
                if c.adapter == "openai_compatible" and c.source == "custom" and c.base_url:
                    return True, None
                return False, None

            usable = {c.id: key_for(c) for c in candidates}
            executable = [c for c in candidates if usable[c.id][0]]
            if not executable:
                execution.reason = (
                    "No key for any candidate model. Add provider keys on your account page, or send "
                    "provider_keys in the request. Returning the recommendation only."
                )
            else:
                if len(executable) < len(candidates):
                    analyzer = request.app.state.analyzer
                    decision = _decide(request, a, body.routing, executable)
                    used = decision
                    routing = analyzer.routing_out(decision)
                    _mark_connected(routing, {c.id for c in executable})
                    if best.chosen and best.chosen.candidate.id != decision.chosen.candidate.id:
                        pick = analyzer._routed(best.chosen)
                        pick.connected = False
                        routing.not_connected = NotConnected(
                            model=pick,
                            instead=routing.recommended,
                            note=_not_connected_note(pick, routing.recommended, used=True),
                        )
                    else:
                        routing.warnings.append(
                            f"Routed among the {len(executable)} model(s) you have keys for "
                            f"(out of {len(candidates)} candidates)."
                        )
                order = [decision.chosen] + [
                    r for r in decision.ranked if r is not decision.chosen and r.capable
                ]
                attempts = []
                for r in order[:3]:
                    c = r.candidate
                    if c.adapter == "openai_compatible":
                        try:
                            await asyncio.to_thread(
                                check_base_url,
                                c.base_url or "",
                                allow_private=s.private_provider_urls_allowed,
                            )
                        except ProviderError as e:
                            execution.attempts.append(Attempt(model=c.id, ok=False, error=e.message))
                            continue
                    attempts.append({"candidate": c, "api_key": usable[c.id][1]})
                won, out, log = (None, None, [])
                called = bool(attempts)
                if attempts:
                    won, out, log = await run_with_fallback(
                        request.app.state.providers,
                        attempts,
                        prompt=body.prompt,
                        system=body.system,
                        max_output_tokens=body.max_output_tokens or s.default_max_output_tokens,
                    )
                execution.attempts += [Attempt(**x) for x in log]
                if won is None:
                    execution.reason = "Every attempted model failed; see attempts."
                else:
                    c = won["candidate"]
                    cost_usd = None
                    if out.input_tokens is not None and out.output_tokens is not None:
                        cost_usd = round(c.cost(out.input_tokens, out.output_tokens), 8)
                    execution = Execution(
                        executed=True,
                        model_used=c.id,
                        provider=c.provider,
                        output=out.text,
                        stop_reason=out.stop_reason,
                        input_tokens=out.input_tokens,
                        output_tokens=out.output_tokens,
                        cost_usd=cost_usd,
                        latency_ms=out.latency_ms,
                        attempts=execution.attempts,
                    )
                    row = used_rows.get(c.id)
                    if row is not None:
                        row.last_used_at = utcnow()

        executed = None
        if called or execution.attempts:
            executed = {"exec_ok": execution.executed, "exec_attempts": len(execution.attempts)}
            if execution.executed:
                won_c = next(c for c in candidates if c.id == execution.model_used)
                executed |= {
                    "executed_model": won_c.id,
                    "executed_provider": won_c.provider,
                    "exec_input_tokens": execution.input_tokens,
                    "exec_output_tokens": execution.output_tokens,
                    "exec_cost_usd": execution.cost_usd,
                }
                if (
                    decision.baseline
                    and execution.input_tokens is not None
                    and execution.output_tokens is not None
                ):
                    executed["exec_baseline_usd"] = decision.baseline.candidate.cost(
                        execution.input_tokens, execution.output_tokens
                    )
        await _record(
            db, request, ctx, a, body.prompt, body.system, body.options.store, best, executed, used=used
        )
        await record_usage(
            db, key_id=ctx.key.id, user_id=ctx.user.id, prompts=1, degraded=int(a.judged.degraded)
        )
        await db.commit()
        scored = request.app.state.analyzer.v1_response(
            a,
            request_id=_rid(request),
            include_suggestions=body.options.include_suggestions,
            include_confidence=body.options.include_confidence,
            stored=body.options.store,
            routing=routing,
        )
        return RouteResponse(**scored.model_dump(), execution=execution)

    # ------------------------------------------------------------ saved provider keys
    def _provider_out(r: ProviderKey) -> dict:
        return {
            "id": r.id,
            "provider": r.provider,
            "label": r.label,
            "key_hint": r.key_hint or None,
            "has_key": r.encrypted_key is not None,
            "base_url": r.base_url,
            "model": r.model,
            "tier": r.tier,
            "input_price": r.input_price,
            "output_price": r.output_price,
            "quality": r.quality,
            "created_at": r.created_at.isoformat(),
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        }

    @router.get("/providers")
    async def list_providers(
        request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
    ):
        return {
            "providers": [_provider_out(r) for r in await _saved_providers(db, user.id)],
            "storage_enabled": _vault(request) is not None,
            "default_quality": request.app.state.config.routing.custom_strength,
        }

    @router.post("/providers", status_code=201)
    async def add_provider(
        request: Request,
        body: ProviderKeyIn,
        user: User = Depends(require_user),
        db: AsyncSession = Depends(get_db),
    ):
        check_csrf(request)
        s = request.app.state.settings
        vault = _vault(request)
        if body.api_key and vault is None:
            raise ApiError(
                400,
                "storage_disabled",
                "This server can't store provider keys (PROVIDER_KEY_SECRET is not set).",
            )
        rows = await _saved_providers(db, user.id)
        if len(rows) >= s.max_provider_keys_per_user:
            raise ApiError(
                400, "too_many_providers", f"You can save up to {s.max_provider_keys_per_user} providers."
            )
        key = (body.api_key or "").strip()
        if body.provider == "openai_compatible":
            missing = [
                f
                for f in ("base_url", "model", "tier", "input_price", "output_price")
                if getattr(body, f) in (None, "")
            ]
            if missing:
                raise ApiError(400, "invalid_request", f"A custom endpoint needs: {', '.join(missing)}.")
            try:
                base_url = await asyncio.to_thread(
                    check_base_url, body.base_url, allow_private=s.private_provider_urls_allowed
                )
            except ProviderError as e:
                raise ApiError(400, "invalid_base_url", e.message) from e
            if any(r.provider == "openai_compatible" and r.model == body.model for r in rows):
                raise ApiError(409, "duplicate_model", f"You already saved a model called {body.model!r}.")
        else:
            if not key:
                raise ApiError(400, "invalid_request", f"An API key is required for {body.provider}.")
            base_url = None
            for r in rows:  # one saved key per built-in provider: replace it
                if r.provider == body.provider:
                    await db.delete(r)
        row = ProviderKey(
            user_id=user.id,
            provider=body.provider,
            label=(body.label or body.model or body.provider.replace("_", " ").title()).strip()[:80],
            encrypted_key=vault.encrypt(key) if key else None,
            key_hint=key_hint(key) if key else "",
            base_url=base_url,
            model=body.model if body.provider == "openai_compatible" else None,
            tier=body.tier if body.provider == "openai_compatible" else None,
            input_price=body.input_price if body.provider == "openai_compatible" else None,
            output_price=body.output_price if body.provider == "openai_compatible" else None,
            quality=body.quality if body.provider == "openai_compatible" else None,
        )
        db.add(row)
        await db.commit()
        return _provider_out(row)

    @router.patch("/providers/{provider_id}")
    async def update_provider(
        request: Request,
        provider_id: int,
        body: ProviderKeyUpdate,
        user: User = Depends(require_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Edit a saved custom endpoint: its quality rating for routing, tier, prices or name."""
        check_csrf(request)
        row = await db.scalar(
            select(ProviderKey).where(ProviderKey.id == provider_id, ProviderKey.user_id == user.id)
        )
        if row is None:
            raise ApiError(404, "not_found", "No such provider.")
        if row.provider != "openai_compatible":
            raise ApiError(
                400, "invalid_request", "Only custom endpoints have a tier, prices and a quality rating."
            )
        changes = body.model_dump(exclude_unset=True)
        for field in ("tier", "input_price", "output_price", "label"):
            if field in changes and changes[field] is None:
                raise ApiError(400, "invalid_request", f"{field} can't be empty.")
        for field, value in changes.items():
            setattr(row, field, value.strip()[:80] if field == "label" else value)
        await db.commit()
        return _provider_out(row)

    @router.delete("/providers/{provider_id}", status_code=204)
    async def delete_provider(
        request: Request,
        provider_id: int,
        user: User = Depends(require_user),
        db: AsyncSession = Depends(get_db),
    ):
        check_csrf(request)
        row = await db.scalar(
            select(ProviderKey).where(ProviderKey.id == provider_id, ProviderKey.user_id == user.id)
        )
        if row is None:
            raise ApiError(404, "not_found", "No such provider.")
        await db.delete(row)
        await db.commit()
        return Response(status_code=204)

    # ------------------------------------------------------------ public info
    @router.get("/pricing")
    async def pricing(request: Request):
        p = request.app.state.config.prices
        return {
            "last_verified": p.last_updated,
            "currency": "USD",
            "unit": "per 1M tokens",
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "provider": m.provider,
                    "input": m.input,
                    "output": m.output,
                    "tier": m.tier,
                    "tokenizer_exact": m.exact and m.tokenizer.startswith("tiktoken"),
                    "note": m.note,
                }
                for m in p.models
            ],
            "default_models": list(p.default_models),
            "output_token_ranges": [list(r) for r in p.output_ranges],
        }

    @router.get("/health")
    async def health(request: Request, db: AsyncSession = Depends(get_db)):
        try:
            await db.execute(text("SELECT 1"))
            database = "ok"
        except Exception:  # noqa: BLE001 - report, don't crash the health check
            database = "unavailable"
        router_ = request.app.state.router
        s = request.app.state.settings
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "backends": {
                "default": "jev" if router_.jev_available else "heuristic",
                "jev": {
                    "configured": router_.jev_available,
                    "model": s.jev_model,
                    # null when the last Jev call worked (or none has run since start-up).
                    "last_error": router_.last_jev_error,
                },
                "heuristic": {"version": HEURISTIC_VERSION},
            },
            "version": request.app.version,
        }

    @router.get("/usage")
    async def usage(
        request: Request,
        days: int = 30,
        user: User | None = Depends(current_user),
        db: AsyncSession = Depends(get_db),
    ):
        key_id = None
        if request.headers.get("authorization"):
            ctx = await require_api_key(request, db)
            user, key_id = ctx.user, ctx.key.id
        if user is None:
            raise ApiError(401, "missing_api_key", "Send an API key, or sign in.")
        days = max(1, min(days, 90))
        since = utcnow().date() - dt.timedelta(days=days - 1)
        q = select(
            UsageDaily.day,
            func.sum(UsageDaily.requests),
            func.sum(UsageDaily.prompts),
            func.sum(UsageDaily.degraded),
        ).where(UsageDaily.user_id == user.id, UsageDaily.day >= since)
        if key_id is not None:
            q = q.where(UsageDaily.key_id == key_id)
        rows = (await db.execute(q.group_by(UsageDaily.day).order_by(UsageDaily.day))).all()
        by_day = {r[0]: r for r in rows}
        series = []
        for i in range(days):
            d = since + dt.timedelta(days=i)
            r = by_day.get(d)
            series.append(
                {
                    "day": d.isoformat(),
                    "requests": int(r[1]) if r else 0,
                    "prompts": int(r[2]) if r else 0,
                    "degraded": int(r[3]) if r else 0,
                }
            )
        plan = request.app.state.config.plans.get(user.plan)
        today = utcnow().date()
        return {
            "scope": "key" if key_id is not None else "account",
            "plan": {
                "name": plan.name,
                "rpm": plan.rpm,
                "daily": plan.daily,
                "monthly": plan.monthly,
                "batch_max": plan.batch_max,
            },
            "used_today": await prompts_used(db, user.id, today),
            "used_this_month": await prompts_used(db, user.id, today.replace(day=1)),
            "days": series,
        }

    @router.get("/usage/routing")
    async def routing_usage(
        request: Request,
        days: int = 30,
        user: User | None = Depends(current_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Where the router sent prompts, per API key and per model, and what that saved.

        `est_*` covers every routed check (recommendations included): the pick's expected cost vs the
        baseline's. `spent_usd` / `saved_usd` cover only real calls made by /v1/route: what the model
        cost at list prices vs the same tokens on the baseline model.
        """
        only_key = None
        if request.headers.get("authorization"):
            ctx = await require_api_key(request, db)
            user, only_key = ctx.user, ctx.key.id
        if user is None:
            raise ApiError(401, "missing_api_key", "Send an API key, or sign in.")
        days = max(1, min(days, 90))
        since = utcnow() - dt.timedelta(days=days)
        kq = select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
        if only_key is not None:
            kq = kq.where(ApiKey.id == only_key)
        keys = list((await db.scalars(kq)).all())
        key_ids = [k.id for k in keys]
        E = ScoreEvent
        where = (E.key_id.in_(key_ids), E.created_at >= since, E.routed_model.is_not(None))
        ok = func.sum(case((E.exec_ok.is_(True), 1), else_=0))
        failed = func.sum(case((E.exec_ok.is_(False), 1), else_=0))
        clar = func.sum(case((E.clarify_first.is_(True), 1), else_=0))
        rows = (
            await db.execute(
                select(
                    E.key_id,
                    E.routed_model,
                    E.routed_provider,
                    E.connected_model,
                    E.executed_model,
                    E.executed_provider,
                    func.count(),
                    ok,
                    failed,
                    clar,
                    func.sum(E.est_cost_usd),
                    func.sum(E.est_baseline_usd),
                    func.sum(E.exec_cost_usd),
                    func.sum(E.exec_baseline_usd),
                    func.sum(E.exec_input_tokens),
                    func.sum(E.exec_output_tokens),
                )
                .where(*where)
                .group_by(
                    E.key_id,
                    E.routed_model,
                    E.routed_provider,
                    E.connected_model,
                    E.executed_model,
                    E.executed_provider,
                )
            )
        ).all()
        base_rows = (
            await db.execute(select(E.baseline_model, func.count()).where(*where).group_by(E.baseline_model))
        ).all()

        names = {m.id: m.name for m in request.app.state.config.prices.models}
        name = lambda m: names.get(m, m)  # noqa: E731

        def blank():
            return {
                "checks": 0, "clarify_first": 0, "sent": 0, "failed": 0,
                "est_cost_usd": 0.0, "est_baseline_usd": 0.0, "spent_usd": 0.0, "exec_baseline_usd": 0.0,
                "input_tokens": 0, "output_tokens": 0,
            }  # fmt: skip

        per_key = {k: blank() | {"recommended": {}, "sent_to": {}, "providers": set()} for k in key_ids}
        per_model: dict[str, dict] = {}
        total = blank()
        for (
            kid,
            rmodel,
            rprov,
            cmodel,
            xmodel,
            xprov,
            n,
            n_ok,
            n_fail,
            n_clar,
            ec,
            eb,
            xc,
            xb,
            ti,
            to,
        ) in rows:
            for bucket in (per_key[kid], total):
                bucket["checks"] += n
                bucket["clarify_first"] += n_clar or 0
                bucket["sent"] += n_ok or 0
                bucket["failed"] += n_fail or 0
                bucket["est_cost_usd"] += ec or 0.0
                bucket["est_baseline_usd"] += eb or 0.0
                bucket["spent_usd"] += xc or 0.0
                bucket["exec_baseline_usd"] += xb or 0.0
                bucket["input_tokens"] += ti or 0
                bucket["output_tokens"] += to or 0
            k = per_key[kid]
            rec = k["recommended"].setdefault(rmodel, {"count": 0, "not_connected": 0, "instead": {}})
            rec["count"] += n
            if cmodel:  # best fit wasn't connected; cmodel is what would be / was used instead
                rec["not_connected"] += n
                rec["instead"][cmodel] = rec["instead"].get(cmodel, 0) + n
            pm = per_model.setdefault(
                rmodel,
                {"model": rmodel, "name": name(rmodel), "provider": rprov, "recommended": 0, "sent": 0,
                 "spent_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
            )  # fmt: skip
            pm["recommended"] += n
            if cmodel:
                pm["not_connected"] = pm.get("not_connected", 0) + n
                cm = per_model.setdefault(
                    cmodel,
                    {"model": cmodel, "name": name(cmodel), "provider": None, "recommended": 0, "sent": 0,
                     "spent_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
                )  # fmt: skip
                cm["used_instead"] = cm.get("used_instead", 0) + n
            if xmodel:
                k["providers"].add(xprov)
                sent = k["sent_to"].setdefault(xmodel, {"calls": 0, "spent_usd": 0.0})
                sent["calls"] += n_ok or 0
                sent["spent_usd"] += xc or 0.0
                xm = per_model.setdefault(
                    xmodel,
                    {"model": xmodel, "name": name(xmodel), "provider": xprov, "recommended": 0, "sent": 0,
                     "spent_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
                )  # fmt: skip
                xm["sent"] += n_ok or 0
                xm["spent_usd"] += xc or 0.0
                xm["input_tokens"] += ti or 0
                xm["output_tokens"] += to or 0

        def finish(b: dict) -> dict:
            out = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in b.items()}
            out["est_saved_usd"] = round(max(0.0, b["est_baseline_usd"] - b["est_cost_usd"]), 6)
            out["saved_usd"] = round(max(0.0, b["exec_baseline_usd"] - b["spent_usd"]), 6)
            return out

        keys_out = []
        for k in keys:
            b = per_key[k.id]
            if k.revoked_at is not None and not b["checks"]:
                continue
            entry = _key_out(k) | finish({x: b[x] for x in blank()})
            entry["recommended"] = sorted(
                (
                    {
                        "model": m,
                        "name": name(m),
                        "count": r["count"],
                        "not_connected": r["not_connected"],
                        "instead": sorted(
                            ({"model": im, "name": name(im), "count": ic} for im, ic in r["instead"].items()),
                            key=lambda x: -x["count"],
                        ),
                    }
                    for m, r in b["recommended"].items()
                ),
                key=lambda x: -x["count"],
            )
            entry["sent_to"] = sorted(
                ({"model": m, "name": name(m), "calls": v["calls"], "spent_usd": round(v["spent_usd"], 6)}
                 for m, v in b["sent_to"].items()),
                key=lambda x: -x["calls"],
            )  # fmt: skip
            entry["providers_used"] = sorted(p for p in b["providers"] if p)
            keys_out.append(entry)
        return {
            "days": days,
            "totals": finish(total),
            "baselines": sorted(
                ({"model": m, "name": name(m), "checks": n} for m, n in base_rows if m),
                key=lambda x: -x["checks"],
            ),
            "models": sorted(
                (
                    {"not_connected": 0, "used_instead": 0, **m, "spent_usd": round(m["spent_usd"], 6)}
                    for m in per_model.values()
                ),
                key=lambda x: (-x["sent"], -(x["recommended"] + x["used_instead"])),
            ),
            "keys": keys_out,
        }

    # ------------------------------------------------------------ accounts
    class Credentials(BaseModel):
        email: str = Field(..., max_length=320)
        password: str = Field(..., max_length=256)
        captcha_token: str | None = None

    def _set_session(request: Request, response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=request.app.state.settings.session_days * 86400,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )

    async def _new_session(db: AsyncSession, request: Request, response: Response, user: User) -> None:
        token, token_hash = new_session_token()
        db.add(
            Session(
                token_hash=token_hash,
                user_id=user.id,
                expires_at=utcnow() + dt.timedelta(days=request.app.state.settings.session_days),
            )
        )
        await db.commit()
        _set_session(request, response, token)

    async def _me(db: AsyncSession, request: Request, user: User) -> dict:
        plan = request.app.state.config.plans.get(user.plan)
        today = utcnow().date()
        n_keys = await db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        )
        return {
            "email": user.email,
            "plan": {
                "name": plan.name,
                "rpm": plan.rpm,
                "daily": plan.daily,
                "monthly": plan.monthly,
                "batch_max": plan.batch_max,
            },
            "created_at": user.created_at.isoformat(),
            "active_keys": int(n_keys or 0),
            "max_keys": request.app.state.config.plans.max_keys_per_user,
            "used_today": await prompts_used(db, user.id, today),
            "used_this_month": await prompts_used(db, user.id, today.replace(day=1)),
        }

    @router.get("/account/config")
    async def account_config(request: Request):
        return {"turnstile_site_key": request.app.state.settings.turnstile_site_key or None}

    @router.post("/account/signup", status_code=201)
    @limiter.limit(lambda: settings.signup_rate_limit)
    async def signup(
        request: Request, response: Response, body: Credentials, db: AsyncSession = Depends(get_db)
    ):
        check_csrf(request)
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise ApiError(400, "invalid_email", "Enter a valid email address.")
        if not 10 <= len(body.password) <= 256:
            raise ApiError(400, "weak_password", "Use a password of at least 10 characters.")
        await verify_turnstile(request, body.captcha_token)
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            plan=request.app.state.config.plans.default_plan,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise ApiError(
                409, "email_taken", "An account with that email already exists. Log in instead."
            ) from None
        await _new_session(db, request, response, user)
        return await _me(db, request, user)

    @router.post("/account/login")
    @limiter.limit(lambda: settings.login_rate_limit)
    async def login(
        request: Request, response: Response, body: Credentials, db: AsyncSession = Depends(get_db)
    ):
        check_csrf(request)
        user = await db.scalar(select(User).where(User.email == body.email.strip().lower()))
        # Always run one password hash so response time doesn't reveal whether the email exists.
        ok = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if user is None or not ok or user.disabled:
            raise ApiError(401, "invalid_credentials", "Wrong email or password.")
        await _new_session(db, request, response, user)
        return await _me(db, request, user)

    @router.post("/account/logout", status_code=204)
    async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
        check_csrf(request)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await db.execute(delete(Session).where(Session.token_hash == sha256_hex(token)))
            await db.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.status_code = 204
        return response

    @router.get("/account/me")
    async def me(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
        return await _me(db, request, user)

    class DeleteAccount(BaseModel):
        password: str = Field(..., max_length=256)

    @router.post("/account/delete", status_code=204)
    async def delete_account(
        request: Request,
        response: Response,
        body: DeleteAccount,
        user: User = Depends(require_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Deletes the account, its keys, usage, events and any stored prompts."""
        check_csrf(request)
        if not verify_password(body.password, user.password_hash):
            raise ApiError(401, "invalid_credentials", "Wrong password.")
        key_ids = select(ApiKey.id).where(ApiKey.user_id == user.id)
        event_ids = select(ScoreEvent.id).where(ScoreEvent.key_id.in_(key_ids))
        # Explicit deletes: SQLite doesn't enforce ON DELETE CASCADE unless configured.
        await db.execute(delete(StoredPrompt).where(StoredPrompt.event_id.in_(event_ids)))
        await db.execute(delete(ScoreEvent).where(ScoreEvent.key_id.in_(key_ids)))
        await db.execute(delete(UsageDaily).where(UsageDaily.user_id == user.id))
        await db.execute(delete(ProviderKey).where(ProviderKey.user_id == user.id))
        await db.execute(delete(ApiKey).where(ApiKey.user_id == user.id))
        await db.execute(delete(Session).where(Session.user_id == user.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.status_code = 204
        return response

    # ------------------------------------------------------------ keys
    class NewKey(BaseModel):
        name: str = Field("Default key", min_length=1, max_length=80)

    def _key_out(k: ApiKey) -> dict:
        return {
            "id": k.id,
            "name": k.name,
            "prefix": k.prefix,
            "masked": mask_key(k.prefix),
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked": k.revoked_at is not None,
        }

    @router.get("/keys")
    async def list_keys(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
        keys = (
            await db.scalars(
                select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
            )
        ).all()
        return {"keys": [_key_out(k) for k in keys]}

    @router.post("/keys", status_code=201)
    async def create_key(
        request: Request, body: NewKey, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
    ):
        check_csrf(request)
        active = await db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        )
        limit = request.app.state.config.plans.max_keys_per_user
        if (active or 0) >= limit:
            raise ApiError(400, "too_many_keys", f"You can have {limit} active keys. Revoke one first.")
        full, prefix, secret_hash = new_api_key()
        key = ApiKey(user_id=user.id, name=body.name.strip(), prefix=prefix, secret_hash=secret_hash)
        db.add(key)
        await db.commit()
        return {**_key_out(key), "key": full, "note": "Copy this key now. It won't be shown again."}

    @router.delete("/keys/{key_id}", status_code=204)
    async def revoke_key(
        request: Request, key_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
    ):
        check_csrf(request)
        key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
        if key is None:
            raise ApiError(404, "not_found", "No such key.")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
            await db.commit()
        return Response(status_code=204)

    return router
