"""Runtime settings, read from the environment (and a .env file at the repo root, if present)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Jev
    typesafe_api_key: str = ""
    jev_model: str = "jev-1.13.0"  # pinned; bump only after re-running the evals
    jev_timeout_s: float = 2.0  # per attempt
    jev_deadline_s: float = 6.0  # whole call, including retries
    jev_max_retries: int = 2

    # Limits
    max_prompt_chars: int = 20_000
    rate_limit: str = "20/hour"  # analyses per IP
    tokens_rate_limit: str = "120/minute"  # live token counter
    playground_rate_limit: str = "60/hour"
    rate_limit_storage: str = "memory://"  # e.g. redis://redis:6379/0 in production

    # Result cache for identical prompts (by hash). Memory only; prompts are never stored.
    cache_ttl_s: int = 3600
    cache_size: int = 512

    # Optional keys for exact token counts on Claude and Gemini models.
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    token_count_timeout_s: float = 1.5

    # CORS: comma-separated origins. Empty means same-origin only (the default deploy).
    allowed_origins: str = ""

    config_dir: Path = Field(default=BACKEND_DIR / "config")
    frontend_dir: Path = Field(default=REPO_DIR / "frontend")

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
