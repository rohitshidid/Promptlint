# PromptLint: FastAPI backend + static frontend in one image.
# Build from the repo root:  docker build -f backend/Dockerfile -t promptlint .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/srv/tiktoken

WORKDIR /srv/backend

# Dependencies first so code edits don't reinstall them.
COPY backend/pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install ".[redis]" && rm -rf app
# Bake the tokenizer in so the container never downloads it at request time.
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

COPY backend/app ./app
COPY backend/config ./config
COPY frontend /srv/frontend

RUN useradd --create-home --uid 10001 promptlint && chown -R promptlint /srv/tiktoken
USER promptlint

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8787\")}/api/health', timeout=2)"

# --proxy-headers so per-IP rate limits see the real client behind Render/Fly load balancers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8787} --proxy-headers --forwarded-allow-ips='*'"]
