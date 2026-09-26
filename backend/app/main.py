"""FastAPI app: the website API (/api), the public API (/v1), the Jev playground proxy, and the static site."""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from limits.storage import storage_from_string
from limits.strategies import MovingWindowRateLimiter
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.analyze import Analyzer
from app.api_v1 import build_v1_router
from app.auth import ApiError
from app.backends import BackendRouter, HeuristicJudge
from app.config import AppConfig, load_config
from app.db import make_engine, make_sessionmaker, prune
from app.jev_client import JevError, JevJudge, Judge
from app.providers import ProviderClient
from app.quiz import build_quiz_router
from app.schemas import AnalyzeRequest, AnalyzeResponse, TokensRequest
from app.security import new_request_id
from app.settings import BACKEND_DIR, Settings, get_settings
from app.tokens import TokenCounter, approx_tokens, tiktoken_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("promptlint")

JEV_UPSTREAM = "https://api.typesafe.ai/v1/systemone"


def run_migrations(database_url: str) -> None:
    """alembic upgrade head, against the app's database URL."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))  # configparser escaping
    command.upgrade(cfg, "head")


def create_app(
    settings: Settings | None = None,
    *,
    judge: Judge | None = None,
    counter: TokenCounter | None = None,
    config: AppConfig | None = None,
    providers: ProviderClient | None = None,
) -> FastAPI:
    """App factory. Tests pass a fake judge; production builds the real Jev judge from settings."""
    settings = settings or get_settings()
    config = config or load_config(settings.config_dir)
    limiter = Limiter(key_func=get_remote_address, storage_uri=settings.rate_limit_storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal judge, counter
        owns_judge = judge is None
        if judge is None:
            if settings.typesafe_api_key:
                judge = JevJudge(
                    api_key=settings.typesafe_api_key,
                    model=settings.jev_model,
                    questions=config.questions,
                    timeout_s=settings.jev_timeout_s,
                    deadline_s=settings.jev_deadline_s,
                    max_retries=settings.jev_max_retries,
                )
            else:
                log.warning(
                    "TYPESAFE_API_KEY is not set: every request uses the heuristic backend (degraded)"
                )
        counter = counter or TokenCounter(
            anthropic_api_key=settings.anthropic_api_key,
            gemini_api_key=settings.gemini_api_key,
            timeout_s=settings.token_count_timeout_s,
        )
        router = BackendRouter(jev=judge, heuristic=HeuristicJudge(config.questions))
        app.state.router = router
        app.state.analyzer = Analyzer(
            router=router,
            counter=counter,
            config=config,
            cache_size=settings.cache_size,
            cache_ttl_s=settings.cache_ttl_s,
        )
        app.state.http = httpx.AsyncClient(timeout=15)
        app.state.providers = providers or ProviderClient(timeout_s=settings.provider_timeout_s)

        if (
            not settings.database_url.startswith("sqlite")
            and settings.prompt_hash_salt == "dev-only-salt-change-me"
        ):
            log.warning("PROMPT_HASH_SALT is the development default; set a long random value in production")
        if settings.auto_migrate:
            await asyncio.to_thread(run_migrations, settings.database_url)
        engine = make_engine(settings.database_url)
        app.state.sessionmaker = make_sessionmaker(engine)

        async def prune_daily() -> None:
            while True:
                try:
                    async with app.state.sessionmaker() as s:
                        stored, events = await prune(
                            s,
                            event_days=settings.event_retention_days,
                            stored_days=settings.stored_prompt_retention_days,
                        )
                    if stored or events:
                        log.info("pruned %d stored prompts and %d events", stored, events)
                except Exception:  # noqa: BLE001 - a failed prune must never take the app down
                    log.exception("prune failed")
                await asyncio.sleep(24 * 3600)

        pruner = asyncio.create_task(prune_daily())
        yield
        pruner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pruner
        await engine.dispose()
        await app.state.http.aclose()
        await app.state.providers.aclose()
        await counter.aclose()
        if owns_judge and isinstance(judge, JevJudge):
            await judge.aclose()

    app = FastAPI(
        title="PromptLint",
        version="1.1.0",
        description="Score a prompt before you send it. See /docs.html for the API guide.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.limiter = limiter
    app.state.settings = settings
    app.state.config = config
    app.state.key_limiter = MovingWindowRateLimiter(storage_from_string(settings.rate_limit_storage))

    if settings.origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
            expose_headers=[
                "X-Request-Id",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
            ],
        )

    # ------------------------------------------------------------------ errors
    def _v1(request: Request) -> bool:
        return request.url.path.startswith("/v1")

    def _v1_error(request: Request, status: int, type_: str, message: str, headers=None) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"type": type_, "message": message, "request_id": request.state.request_id}},
            headers=headers,
        )

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _v1_error(request, exc.status, exc.type, exc.message, exc.headers)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        if _v1(request):
            return _v1_error(
                request, 429, "rate_limit_exceeded", f"Too many attempts ({exc.detail}). Try again later."
            )
        return JSONResponse(
            status_code=429,
            content={
                "error": "You've hit the analysis limit for now.",
                "detail": f"PromptLint allows {exc.detail} per visitor to keep the demo free. Try again later, "
                "or create a free API key.",
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        msg = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        if _v1(request):
            loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
            return _v1_error(request, 400, "invalid_request", f"{loc}: {msg}" if loc else msg)
        return JSONResponse(status_code=422, content={"error": msg, "detail": None})

    @app.exception_handler(JevError)
    async def _jev_failed(request: Request, exc: JevError):
        if _v1(request):
            return _v1_error(request, 503, "backend_unavailable", exc.message)
        return JSONResponse(status_code=exc.status, content={"error": exc.message, "detail": None})

    # --------------------------------------------------------- website API
    @app.get("/api/health")
    async def health(request: Request):
        return {
            "ok": True,
            "jev_configured": request.app.state.router.jev_available,
            "jev_model": settings.jev_model,
        }

    @app.get("/api/models")
    async def models():
        p = config.prices
        return {
            "last_updated": p.last_updated,
            "default_models": list(p.default_models),
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "provider": m.provider,
                    "input": m.input,
                    "output": m.output,
                    "tier": m.tier,
                    "note": m.note,
                }
                for m in p.models
            ],
            "max_prompt_chars": settings.max_prompt_chars,
            "jev_configured": app.state.router.jev_available if hasattr(app.state, "router") else False,
        }

    @app.post("/api/analyze", response_model=AnalyzeResponse, responses={413: {}, 422: {}, 429: {}, 503: {}})
    @limiter.limit(lambda: settings.rate_limit)
    async def analyze(request: Request, body: AnalyzeRequest):
        if len(body.prompt) > settings.max_prompt_chars:
            return JSONResponse(
                status_code=413,
                content={
                    "error": f"That prompt is {len(body.prompt):,} characters. The limit is {settings.max_prompt_chars:,}.",
                    "detail": None,
                },
            )
        unknown = [m for m in body.models or [] if config.prices.get(m) is None]
        if unknown:
            return JSONResponse(
                status_code=422, content={"error": f"Unknown model: {unknown[0]}", "detail": None}
            )
        analyzer: Analyzer = request.app.state.analyzer
        return analyzer.web_report(
            await analyzer.analyze(body.prompt, body.models, backend=body.backend), strategy=body.strategy
        )

    @app.post("/api/tokens")
    @limiter.limit(lambda: settings.tokens_rate_limit)
    async def tokens(request: Request, body: TokensRequest):
        """Live input-token counter for the editor. Local only: no Jev call, no provider call."""
        return {
            "tokens": tiktoken_count(body.prompt),
            "approx_chars4": approx_tokens(body.prompt),
            "encoding": "o200k_base",
        }

    # ------------------------------------------------------------ public API
    app.include_router(build_v1_router(limiter, settings))
    app.include_router(build_quiz_router(limiter, settings))

    # ------------------------------------------------ Jev playground (/playground)
    @app.get("/api/config")
    async def playground_config():
        return {"serverKey": bool(settings.typesafe_api_key)}

    @app.post("/api/systemone")
    @limiter.limit(lambda: settings.playground_rate_limit)
    async def playground_proxy(request: Request):
        # With the server's key in use, only same-origin pages may call this, so another website
        # open in the visitor's browser can't spend the key through the proxy.
        origin = request.headers.get("origin")
        own_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        page_key = request.headers.get("authorization")
        if not page_key and origin and origin != own_origin and origin not in settings.origins:
            return JSONResponse(status_code=403, content={"error": "Cross-origin requests are blocked."})
        auth = page_key or (f"Bearer {settings.typesafe_api_key}" if settings.typesafe_api_key else "")
        body = await request.body()
        if len(body) > 200_000:
            return JSONResponse(status_code=413, content={"error": "Request too large."})
        try:
            upstream = await request.app.state.http.post(
                JEV_UPSTREAM,
                content=body,
                headers={"Content-Type": "application/json", "Authorization": auth},
            )
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=502, content={"error": "Could not reach the Jev API", "detail": str(e)}
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    # ------------------------------------------------------------ static site
    if Path(settings.frontend_dir).is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="site")

    # Key-authenticated /v1 endpoints can be called from any website (they use Bearer keys, not cookies,
    # so there's nothing for another origin to borrow). Account and key-management endpoints stay
    # same-origin only; no Allow-Credentials is ever sent, so browsers never attach cookies cross-site.
    public_api = ("/v1/score", "/v1/route", "/v1/pricing", "/v1/health", "/v1/usage")
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Expose-Headers": "X-Request-Id, X-RateLimit-Limit, X-RateLimit-Remaining, "
        "X-RateLimit-Reset, X-Quota-Limit, X-Quota-Remaining, X-Quota-Period, Retry-After",
        "Access-Control-Max-Age": "600",
    }

    @app.middleware("http")
    async def _public_api_cors(request: Request, call_next):
        is_public = request.url.path.startswith(public_api)
        if is_public and request.method == "OPTIONS":
            return Response(status_code=204, headers=cors_headers)
        response = await call_next(request)
        if is_public:
            response.headers.update(cors_headers)
        return response

    @app.middleware("http")
    async def _request_id_and_headers(request: Request, call_next):
        request.state.request_id = new_request_id()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.url.path.startswith(("/v1/account", "/v1/keys")):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.endswith((".html", "/")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    return app


app = create_app()
