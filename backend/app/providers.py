"""Calls LLM providers on the user's behalf (the /v1/route pipeline).

  anthropic          Claude, through the official Anthropic SDK
  openai             OpenAI chat completions
  gemini             Google Gemini generateContent
  openai_compatible  any OpenAI-style /chat/completions endpoint: Ollama, Groq, OpenRouter, Together,
                     vLLM, LM Studio… (base URL + model name + optional key)

Keys come from the request or from the user's saved (encrypted) provider keys. They are never
logged or returned. User-supplied base URLs are checked so the server can't be pointed at private
or internal addresses (SSRF) unless ALLOW_PRIVATE_PROVIDER_URLS is on (local development).
"""

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ADAPTERS = ("anthropic", "openai", "gemini", "openai_compatible")


class ProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    stop_reason: str | None = None


# ---------------------------------------------------------------- URL safety (SSRF)
def check_base_url(url: str, *, allow_private: bool) -> str:
    """Validate a user-supplied provider base URL. Returns it without a trailing slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProviderError(
            "base_url must be a full http(s) URL, e.g. https://api.groq.com/openai/v1", retryable=False
        )
    if parts.username or parts.password:
        raise ProviderError("Don't put credentials in base_url; use the api_key field.", retryable=False)
    if not allow_private:
        if parts.scheme != "https":
            raise ProviderError("base_url must use https.", retryable=False)
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise ProviderError(f"Can't resolve {parts.hostname}.", retryable=False) from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise ProviderError(
                    f"{parts.hostname} points to a private address. A hosted server can't reach your own machine: "
                    "expose it with a public HTTPS URL (for example a tunnel) instead.",
                    retryable=False,
                )
    return url.strip().rstrip("/")


def _error_from_status(provider: str, status: int, body: str) -> ProviderError:
    if status in (401, 403):
        return ProviderError(
            f"{provider} rejected the API key (HTTP {status}).", status=status, retryable=False
        )
    if status == 404:
        return ProviderError(f"{provider} doesn't know that model (HTTP 404).", status=status, retryable=True)
    if status == 400:
        return ProviderError(f"{provider} rejected the request: {body[:200]}", status=status, retryable=False)
    return ProviderError(f"{provider} returned HTTP {status}.", status=status, retryable=True)


class ProviderClient:
    """One shared HTTP client for OpenAI, Gemini and compatible endpoints; the SDK for Anthropic."""

    def __init__(self, *, timeout_s: float = 120.0, transport: httpx2.AsyncBaseTransport | None = None):
        self._timeout = timeout_s
        self._transport = transport  # tests inject a MockTransport
        self._http = httpx2.AsyncClient(timeout=timeout_s, transport=transport, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(
        self,
        *,
        adapter: str,
        model: str,
        prompt: str,
        system: str | None,
        api_key: str | None,
        max_output_tokens: int,
        base_url: str | None = None,
    ) -> Completion:
        start = time.perf_counter()
        if adapter == "anthropic":
            out = await self._anthropic(model, prompt, system, api_key, max_output_tokens)
        elif adapter == "openai":
            out = await self._chat(
                OPENAI_URL, "OpenAI", model, prompt, system, api_key, max_output_tokens, openai=True
            )
        elif adapter == "gemini":
            out = await self._gemini(model, prompt, system, api_key, max_output_tokens)
        elif adapter == "openai_compatible":
            if not base_url:
                raise ProviderError("This model has no base_url.", retryable=False)
            out = await self._chat(
                f"{base_url}/chat/completions",
                "The endpoint",
                model,
                prompt,
                system,
                api_key,
                max_output_tokens,
            )
        else:
            raise ProviderError(f"Unknown provider {adapter!r}.", retryable=False)
        text, tin, tout, stop = out
        return Completion(text, tin, tout, round((time.perf_counter() - start) * 1000), stop)

    # ------------------------------------------------------------ Anthropic (official SDK)
    async def _anthropic(self, model, prompt, system, api_key, max_tokens):
        import anthropic

        if not api_key:
            raise ProviderError("No Anthropic API key.", retryable=False)
        http_client = (
            httpx2.AsyncClient(timeout=self._timeout, transport=self._transport) if self._transport else None
        )
        client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=1, timeout=self._timeout, http_client=http_client
        )
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            kwargs["system"] = system
        try:
            msg = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic rejected the API key.", status=401, retryable=False) from e
        except anthropic.PermissionDeniedError as e:
            raise ProviderError("This Anthropic key can't use that model.", status=403, retryable=True) from e
        except anthropic.NotFoundError as e:
            raise ProviderError("Anthropic doesn't know that model.", status=404, retryable=True) from e
        except anthropic.BadRequestError as e:
            raise ProviderError(
                f"Anthropic rejected the request: {e.message[:200]}", status=400, retryable=False
            ) from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Anthropic rate limit reached.", status=429, retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(
                f"Anthropic returned HTTP {e.status_code}.", status=e.status_code, retryable=True
            ) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Couldn't reach Anthropic.", retryable=True) from e
        finally:
            await client.close()
        if msg.stop_reason == "refusal":
            raise ProviderError("The Claude model declined this request.", retryable=True)
        text = "".join(b.text for b in msg.content if b.type == "text")
        return text, msg.usage.input_tokens, msg.usage.output_tokens, msg.stop_reason

    # ------------------------------------------------------------ OpenAI and compatible
    async def _chat(self, url, label, model, prompt, system, api_key, max_tokens, *, openai=False):
        if openai and not api_key:
            raise ProviderError("No OpenAI API key.", retryable=False)
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body = {"model": model, "messages": messages}
        body["max_completion_tokens" if openai else "max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            r = await self._http.post(url, json=body, headers=headers)
        except httpx2.TimeoutException as e:
            raise ProviderError(f"{label} took too long to answer.", retryable=True) from e
        except httpx2.HTTPError as e:
            raise ProviderError(f"Couldn't reach {label.lower()}.", retryable=True) from e
        if r.status_code >= 300:
            raise _error_from_status(label, r.status_code, r.text)
        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            usage = data.get("usage") or {}
            return (
                text,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                choice.get("finish_reason"),
            )
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"{label} sent a response we couldn't read.", retryable=True) from e

    # ------------------------------------------------------------ Gemini
    async def _gemini(self, model, prompt, system, api_key, max_tokens):
        if not api_key:
            raise ProviderError("No Gemini API key.", retryable=False)
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        try:
            r = await self._http.post(
                GEMINI_URL.format(model=model), json=body, headers={"x-goog-api-key": api_key}
            )
        except httpx2.TimeoutException as e:
            raise ProviderError("Gemini took too long to answer.", retryable=True) from e
        except httpx2.HTTPError as e:
            raise ProviderError("Couldn't reach Gemini.", retryable=True) from e
        if r.status_code >= 300:
            raise _error_from_status("Gemini", r.status_code, r.text)
        try:
            data = r.json()
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
            usage = data.get("usageMetadata") or {}
            return (
                text,
                usage.get("promptTokenCount"),
                usage.get("candidatesTokenCount"),
                cand.get("finishReason"),
            )
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError("Gemini sent a response we couldn't read.", retryable=True) from e


async def run_with_fallback(
    client: ProviderClient, attempts: list[dict], *, prompt: str, system: str | None, max_output_tokens: int
) -> tuple[dict | None, Completion | None, list[dict]]:
    """Try each attempt ({candidate, api_key}) in order; stop at the first success.

    Returns (winning attempt, completion, log). A non-retryable error on one model (bad key, bad request)
    still moves on to the next model, since that one may use a different provider.
    """
    log: list[dict] = []
    for att in attempts:
        c = att["candidate"]
        try:
            out = await asyncio.wait_for(
                client.complete(
                    adapter=c.adapter,
                    model=c.api_model or c.id,
                    prompt=prompt,
                    system=system,
                    api_key=att["api_key"],
                    max_output_tokens=max_output_tokens,
                    base_url=c.base_url,
                ),
                timeout=client._timeout + 5,
            )
            log.append({"model": c.id, "ok": True})
            return att, out, log
        except TimeoutError:
            log.append({"model": c.id, "ok": False, "error": "Timed out."})
        except ProviderError as e:
            log.append({"model": c.id, "ok": False, "error": e.message})
    return None, None, log
