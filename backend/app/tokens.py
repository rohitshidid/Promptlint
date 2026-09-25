"""Input-token counting per model: exact where the provider allows it, labeled approximate otherwise.

  tiktoken:<encoding>  local tiktoken (exact unless prices.yaml marks the encoding as a stand-in)
  anthropic            Anthropic count_tokens endpoint, when ANTHROPIC_API_KEY is set
  gemini               Gemini countTokens endpoint, when GEMINI_API_KEY is set
  otherwise            ceil(chars / 4), approximate

Provider counts are cached per (prompt hash, model); the prompt itself is never kept.
"""

import asyncio
import hashlib
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

import httpx
import tiktoken

from app.config import ModelPrice

log = logging.getLogger("promptlint.tokens")


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    exact: bool


def approx_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


@lru_cache(maxsize=8)
def _encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


def tiktoken_count(text: str, encoding: str = "o200k_base") -> int:
    return len(_encoding(encoding).encode(text, disallowed_special=()))


class TokenCounter:
    def __init__(
        self,
        *,
        anthropic_api_key: str = "",
        gemini_api_key: str = "",
        timeout_s: float = 1.5,
        cache_size: int = 2048,
    ):
        self._anthropic_key = anthropic_api_key
        self._gemini_key = gemini_api_key
        self._timeout = timeout_s
        self._cache: OrderedDict[tuple[str, str], TokenCount] = OrderedDict()
        self._cache_size = cache_size
        self._anthropic = None
        self._http: httpx.AsyncClient | None = None

    async def count(self, text: str, model: ModelPrice) -> TokenCount:
        kind = model.tokenizer
        if kind.startswith("tiktoken:"):
            return TokenCount(tiktoken_count(text, kind.split(":", 1)[1]), model.exact)

        key = (hashlib.sha256(text.encode()).hexdigest(), model.id)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        result: TokenCount | None = None
        try:
            if kind == "anthropic" and self._anthropic_key:
                result = await asyncio.wait_for(self._count_anthropic(text, model.id), self._timeout)
            elif kind == "gemini" and self._gemini_key:
                result = await asyncio.wait_for(self._count_gemini(text, model.id), self._timeout)
        except Exception as e:  # any provider failure falls back to the labeled estimate
            log.warning("token count for %s failed, using approx.: %s", model.id, type(e).__name__)

        if result is None:
            return TokenCount(approx_tokens(text), False)
        self._cache[key] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    async def _count_anthropic(self, text: str, model_id: str) -> TokenCount:
        if self._anthropic is None:
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(api_key=self._anthropic_key, max_retries=0)
        resp = await self._anthropic.messages.count_tokens(
            model=model_id, messages=[{"role": "user", "content": text}]
        )
        return TokenCount(resp.input_tokens, True)

    async def _count_gemini(self, text: str, model_id: str) -> TokenCount:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        resp = await self._http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:countTokens",
            headers={"x-goog-api-key": self._gemini_key},
            json={"contents": [{"parts": [{"text": text}]}]},
        )
        resp.raise_for_status()
        return TokenCount(int(resp.json()["totalTokens"]), True)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._anthropic is not None:
            await self._anthropic.close()
