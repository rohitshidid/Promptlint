"""Database models and helpers (accounts, API keys, usage metering).

SQLite for local development and tests; Postgres in production. The free Neon tier is the target:
0.5 GB, so the tables hold metadata only, and old events are pruned daily.
Schema changes go through Alembic (backend/migrations).
"""

import datetime as dt
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(32), default="free")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie value
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of the secret part
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageDaily(Base):
    """Per key per UTC day. Quotas are account-wide, so they sum over the user's keys."""

    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("key_id", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    prompts: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[int] = mapped_column(Integer, default=0)


class ScoreEvent(Base):
    """One scored prompt. Metadata only: a salted hash, never the text (PQS §12)."""

    __tablename__ = "score_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(40), index=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    prompt_chars: Mapped[int] = mapped_column(Integer)
    backend: Mapped[str] = mapped_column(String(32))
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    lint_score: Mapped[int] = mapped_column(Integer)
    pqs_score: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    # Routing (v1.2): what the router picked, and what really happened when /v1/route called a model.
    strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    task_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routed_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    routed_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # the best of your connected models, when the best fit (routed_model) isn't one you have a key for
    connected_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baseline_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    est_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # the pick, p50
    est_baseline_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # baseline, p50
    clarify_first: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exec_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = no model was called
    executed_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    executed_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exec_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_baseline_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # same tokens on the baseline


class StoredPrompt(Base):
    """Prompt text, kept only when the caller opts in with `options.store: true`."""

    __tablename__ = "stored_prompts"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("score_events.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    system: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProviderKey(Base):
    """A user's LLM provider credential for the routing pipeline, encrypted at rest.

    Built-in providers (anthropic, openai, gemini) unlock every catalog model from that provider.
    An openai_compatible entry describes one model at a custom endpoint (Ollama, Groq, OpenRouter…).
    """

    __tablename__ = "provider_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # anthropic | openai | gemini | openai_compatible
    label: Mapped[str] = mapped_column(String(80))
    encrypted_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # None: endpoint needs no key (Ollama)
    key_hint: Mapped[str] = mapped_column(String(16), default="")
    # openai_compatible only
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    input_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)  # your 0–1 rating for routing
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizResult(Base):
    """One finished prompt-engineering quiz. Only Jev-scored runs are ranked."""

    __tablename__ = "quiz_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nickname: Mapped[str] = mapped_column(String(24), index=True)
    score: Mapped[int] = mapped_column(Integer, index=True)
    grade: Mapped[str] = mapped_column(String(4))
    rounds: Mapped[str] = mapped_column(Text)  # JSON list of per-round scores
    ranked: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ---------------------------------------------------------------- engine
def normalize_url(url: str) -> tuple[str, dict]:
    """Accept the URLs people paste (Neon, Render, Heroku style) and return an async URL + connect args.

    postgres:// and postgresql:// become postgresql+asyncpg://. asyncpg doesn't understand libpq's
    `sslmode`/`channel_binding` query params, so they are translated into `connect_args`.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    if not url.startswith("postgresql+asyncpg://"):
        return url, {}
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    connect_args: dict = {}
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    if sslmode in ("require", "verify-ca", "verify-full"):
        connect_args["ssl"] = True
    return urlunsplit(parts._replace(query=urlencode(query))), connect_args


def make_engine(url: str) -> AsyncEngine:
    url, connect_args = normalize_url(url)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url, connect_args={"timeout": 15})
    # Neon scales to zero; pre-ping revives connections dropped while it slept.
    return create_async_engine(
        url, connect_args=connect_args, pool_pre_ping=True, pool_size=5, max_overflow=5
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as s:
        yield s


# ---------------------------------------------------------------- usage
async def record_usage(s: AsyncSession, *, key_id: int, user_id: int, prompts: int, degraded: int) -> None:
    """Upsert today's counters for a key (works on SQLite and Postgres)."""
    day = utcnow().date()
    values = {
        "key_id": key_id,
        "user_id": user_id,
        "day": day,
        "requests": 1,
        "prompts": prompts,
        "degraded": degraded,
    }
    dialect = s.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(UsageDaily).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key_id", "day"],
        set_={
            "requests": UsageDaily.requests + 1,
            "prompts": UsageDaily.prompts + prompts,
            "degraded": UsageDaily.degraded + degraded,
        },
    )
    await s.execute(stmt)


async def prompts_used(s: AsyncSession, user_id: int, since: dt.date) -> int:
    total = await s.scalar(
        select(func.coalesce(func.sum(UsageDaily.prompts), 0)).where(
            UsageDaily.user_id == user_id, UsageDaily.day >= since
        )
    )
    return int(total or 0)


async def prune(s: AsyncSession, *, event_days: int, stored_days: int) -> tuple[int, int]:
    """Delete old events, stored prompts and expired sessions so the free database stays small."""
    now = utcnow()
    r1 = await s.execute(
        delete(StoredPrompt).where(StoredPrompt.created_at < now - dt.timedelta(days=stored_days))
    )
    r2 = await s.execute(
        delete(ScoreEvent).where(ScoreEvent.created_at < now - dt.timedelta(days=event_days))
    )
    await s.execute(delete(Session).where(Session.expires_at < now))
    await s.commit()
    return r1.rowcount or 0, r2.rowcount or 0
