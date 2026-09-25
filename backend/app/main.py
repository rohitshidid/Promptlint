"""FastAPI app: /api/analyze, supporting endpoints, the Jev playground proxy, and the static site."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.analyze import Analyzer
from app.config import AppConfig, load_config
from app.jev_client import JevError, JevJudge, Judge
from app.schemas import AnalyzeRequest, AnalyzeResponse, TokensRequest
from app.settings import Settings, get_settings
from app.tokens import TokenCounter, approx_tokens, tiktoken_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("promptlint")

JEV_UPSTREAM = "https://api.typesafe.ai/v1/systemone"


def create_app(
    settings: Settings | None = None,
    *,
    judge: Judge | None = None,
    counter: TokenCounter | None = None,
    config: AppConfig | None = None,
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
            if not settings.typesafe_api_key:
                log.warning("TYPESAFE_API_KEY is not set: /api/analyze will return 503")
            else:
                judge = JevJudge(
                    api_key=settings.typesafe_api_key,
                    model=settings.jev_model,
                    questions=config.questions,
                    timeout_s=settings.jev_timeout_s,
                    deadline_s=settings.jev_deadline_s,
                    max_retries=settings.jev_max_retries,
                )
        counter = counter or TokenCounter(
            anthropic_api_key=settings.anthropic_api_key,
            gemini_api_key=settings.gemini_api_key,
            timeout_s=settings.token_count_timeout_s,
        )
        app.state.analyzer = (
            Analyzer(
                judge=judge,
                counter=counter,
                config=config,
                cache_size=settings.cache_size,
                cache_ttl_s=settings.cache_ttl_s,
            )
            if judge is not None
            else None
        )
        app.state.http = httpx.AsyncClient(timeout=15)
        yield
        await app.state.http.aclose()
        await counter.aclose()
        if owns_judge and isinstance(judge, JevJudge):
            await judge.aclose()

    app = FastAPI(
        title="PromptLint",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.limiter = limiter

    if settings.origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    # ------------------------------------------------------------------ errors
    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={
                "error": "You've hit the analysis limit for now.",
                "detail": f"PromptLint allows {exc.detail} per visitor to keep the demo free. "
                "Try again later.",
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        msg = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        return JSONResponse(status_code=422, content={"error": msg, "detail": None})

    @app.exception_handler(JevError)
    async def _jev_failed(request: Request, exc: JevError):
        return JSONResponse(status_code=exc.status, content={"error": exc.message, "detail": None})

    # --------------------------------------------------------------- endpoints
    @app.get("/api/health")
    async def health(request: Request):
        return {
            "ok": True,
            "jev_configured": request.app.state.analyzer is not None,
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
        }

    @app.post("/api/analyze", response_model=AnalyzeResponse, responses={422: {}, 429: {}, 503: {}, 504: {}})
    @limiter.limit(lambda: settings.rate_limit)
    async def analyze(request: Request, body: AnalyzeRequest):
        if len(body.prompt) > settings.max_prompt_chars:
            return JSONResponse(
                status_code=413,
                content={
                    "error": f"That prompt is {len(body.prompt):,} characters. "
                    f"The limit is {settings.max_prompt_chars:,}.",
                    "detail": None,
                },
            )
        unknown = [m for m in body.models or [] if config.prices.get(m) is None]
        if unknown:
            return JSONResponse(
                status_code=422, content={"error": f"Unknown model: {unknown[0]}", "detail": None}
            )
        analyzer: Analyzer | None = request.app.state.analyzer
        if analyzer is None:
            return JSONResponse(
                status_code=503,
                content={"error": "The server has no TYPESAFE_API_KEY configured.", "detail": None},
            )
        return await analyzer.analyze(body.prompt, body.models)

    @app.post("/api/tokens")
    @limiter.limit(lambda: settings.tokens_rate_limit)
    async def tokens(request: Request, body: TokensRequest):
        """Live input-token counter for the editor. Local only: no Jev call, no provider call."""
        return {
            "tokens": tiktoken_count(body.prompt),
            "approx_chars4": approx_tokens(body.prompt),
            "encoding": "o200k_base",
        }

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
    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="site")

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.url.path.endswith((".html", "/")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    return app


app = create_app()
