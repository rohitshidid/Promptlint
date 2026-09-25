"""Public API, /v1 (prompt-quality-scorer.md §11), plus self-serve accounts and key management.

POST   /v1/score            score one prompt                          API key
POST   /v1/score/batch      score up to 50 prompts (plan limit)       API key
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
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.schemas import BatchRequest, BatchResponse, BatchResult, ScoreRequest, ScoreResponse
from app.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
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
                f"Your plan allows {ctx.plan.rpm} requests per minute. Retry in {retry}s.",
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
                    f"{limit}/{period} remain on the {ctx.plan.name} plan.",
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
        db: AsyncSession, request: Request, ctx: KeyContext, a: Analysis, prompt: str, system, store: bool
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
        )
        db.add(event)
        if store:
            await db.flush()
            db.add(StoredPrompt(event_id=event.id, prompt=prompt, system=system))

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
        await _quota(db, response, ctx, 1)
        a = await _analyze(request, body.prompt, body.system, body.models, body.backend)
        await _record(db, request, ctx, a, body.prompt, body.system, body.options.store)
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
            await _record(db, request, ctx, a, item.prompt, item.system, body.options.store)
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
                    ),
                )
            )
        if scored:
            await record_usage(db, key_id=ctx.key.id, user_id=ctx.user.id, prompts=scored, degraded=degraded)
        await db.commit()
        return BatchResponse(
            id=rid.replace("req_", "bat_", 1), results=results, scored=scored, failed=len(results) - scored
        )

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
                "jev": {"configured": router_.jev_available, "model": s.jev_model},
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
