"""Request authentication for the public API and the account dashboard.

  API key  `Authorization: Bearer pqs_live_…`  → /v1/score, /v1/score/batch, /v1/usage
  Session  HttpOnly cookie from sign-up/log-in  → /v1/account/*, /v1/keys, /v1/usage

Cookie-authenticated writes also need the `X-PL-CSRF: 1` header and a same-origin `Origin`.
A cross-site page can't add a custom header without a CORS preflight, which this server never
approves, so that blocks cross-site request forgery.
"""

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Plan
from app.db import ApiKey, Session, User, utcnow
from app.security import parse_api_key, sha256_hex, verify_secret

SESSION_COOKIE = "pl_session"
CSRF_HEADER = "x-pl-csrf"


class ApiError(Exception):
    """Rendered as {"error": {"type", "message", "request_id"}} (prompt-quality-scorer.md §11)."""

    def __init__(self, status: int, type_: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.message = message
        self.headers = headers or {}


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as s:
        yield s


# ---------------------------------------------------------------- API keys
@dataclass
class KeyContext:
    key: ApiKey
    user: User
    plan: Plan


async def require_api_key(request: Request, db: AsyncSession = Depends(get_db)) -> KeyContext:
    header = request.headers.get("authorization", "").strip()
    if header.lower() == "bearer":
        header = "Bearer "  # nothing after Bearer: explained below
    if not header.lower().startswith("bearer "):
        raise ApiError(401, "missing_api_key", "Send your API key as 'Authorization: Bearer pqs_live_…'.")
    token = header[7:].strip()
    if not token:
        # Usually `Bearer $pqs_live_…` in a shell: `$` expands an (unset) variable to nothing.
        raise ApiError(
            401,
            "missing_api_key",
            "Nothing after 'Bearer'. If you typed $ before the key, your shell treated it as a variable: "
            "remove the $, or run  export PQS_KEY=pqs_live_…  and use $PQS_KEY.",
        )
    parsed = parse_api_key(token)
    if parsed is None:
        raise ApiError(
            401, "invalid_api_key", "That API key is malformed. Keys look like pqs_live_<8 chars>_<secret>."
        )
    prefix, secret = parsed
    row = (
        await db.execute(
            select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.prefix == prefix)
        )
    ).first()
    # Same error for unknown, wrong-secret and revoked keys, so keys can't be probed.
    if row is None or not verify_secret(secret, row.ApiKey.secret_hash) or row.ApiKey.revoked_at is not None:
        raise ApiError(401, "invalid_api_key", "That API key is invalid or has been revoked.")
    key, user = row.ApiKey, row.User
    if user.disabled:
        raise ApiError(403, "account_disabled", "This account is disabled.")
    now = utcnow()
    last = key.last_used_at
    if last is not None and last.tzinfo is None:  # SQLite returns naive datetimes
        last = last.replace(tzinfo=dt.UTC)
    if last is None or now - last > dt.timedelta(minutes=1):
        key.last_used_at = now
        await db.commit()
    return KeyContext(key=key, user=user, plan=request.app.state.config.plans.get(user.plan))


# ---------------------------------------------------------------- sessions
async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = (
        await db.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == sha256_hex(token))
        )
    ).first()
    if row is None:
        return None
    expires = row.Session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    if expires < utcnow() or row.User.disabled:
        return None
    return row.User


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise ApiError(401, "not_signed_in", "Sign in to manage keys and see usage.")
    return user


def check_csrf(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != "1":
        raise ApiError(403, "csrf_failed", "Missing the X-PL-CSRF header.")
    origin = request.headers.get("origin")
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin and origin != own and origin not in request.app.state.settings.origins:
        raise ApiError(403, "csrf_failed", "Cross-origin requests are not allowed here.")


def cookie_secure(request: Request) -> bool:
    configured = request.app.state.settings.cookie_secure
    return request.url.scheme == "https" if configured is None else configured


# ---------------------------------------------------------------- Turnstile (optional, free)
async def verify_turnstile(request: Request, token: str | None) -> None:
    secret = request.app.state.settings.turnstile_secret_key
    if not secret:
        return
    if not token:
        raise ApiError(400, "captcha_required", "Please complete the captcha.")
    try:
        r = await request.app.state.http.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": secret,
                "response": token,
                "remoteip": request.client.host if request.client else "",
            },
        )
        ok = r.json().get("success") is True
    except (httpx.HTTPError, ValueError):
        ok = False
    if not ok:
        raise ApiError(400, "captcha_failed", "The captcha check failed. Please try again.")
