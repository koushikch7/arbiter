from abc import ABC, abstractmethod
from typing import List, Optional
from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, Message


class RateLimitError(Exception):
    """Raised when an API key hits its rate limit.

    Carries an optional retry_after (seconds) parsed from the upstream
    error body / headers so the router can set a tight cooldown instead of
    the hardcoded 300 s default.
    """

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(headers: dict | None, body_text: str | None = None) -> float | None:
    """Extract a retry-after value (seconds) from response headers or a body.

    Looks at the Retry-After header first (RFC 7231 — either a delta in
    seconds or an HTTP-date), then scrapes the upstream JSON body for the
    common "Please try again in X.Xs" / "reset after Xms" patterns
    that Groq, Gemini, and OpenAI emit. Returns None if nothing parseable.
    """
    import re
    import email.utils
    import time as _time
    if headers:
        # case-insensitive lookup
        h = {k.lower(): v for k, v in headers.items()}
        ra = h.get("retry-after")
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                # HTTP-date
                try:
                    parsed = email.utils.parsedate_to_datetime(ra)
                    return max(0.0, (parsed.timestamp() - _time.time()))
                except Exception:
                    pass
    if body_text:
        m = re.search(r"try again in\s+([0-9.]+)\s*s", body_text, re.IGNORECASE)
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except ValueError:
                pass
        m = re.search(r"try again in\s+([0-9]+)\s*ms", body_text, re.IGNORECASE)
        if m:
            try:
                return max(0.0, float(m.group(1)) / 1000.0)
            except ValueError:
                pass
        m = re.search(r"reset(?:s)?\s+(?:in|after)\s+([0-9.]+)\s*s", body_text, re.IGNORECASE)
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except ValueError:
                pass
    return None


class ProviderError(Exception):
    """Raised when a provider returns an unexpected error."""
    pass


class BaseProvider(ABC):
    name: str = ""
    models: List[str] = []
    max_context_tokens: int = 4096

    # Subclasses may set this to the upstream OpenAI-compatible
    # ``/v1/models`` URL.  When set, the default ``fetch_models()``
    # implementation will GET it with a Bearer token and parse the
    # standard ``{"data": [{"id": ...}, ...]}`` shape.  Providers with
    # non-standard catalog endpoints can ignore this and override
    # ``fetch_models()`` directly.
    models_discovery_url: Optional[str] = None

    @abstractmethod
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Send a chat completion request to the provider and return an OpenAI-compatible response."""
        ...

    # ------------------------------------------------------------------
    # Optional: native SSE streaming
    # ------------------------------------------------------------------
    async def complete_stream(
        self, request: "ChatCompletionRequest", api_key: str
    ):
        """
        Optional — yield ``chat.completion.chunk`` dicts as they arrive
        from the upstream provider over SSE.

        Subclasses that wrap an OpenAI-compatible upstream should override
        this to call ``app.streaming.openai_stream.stream_openai_chat()``.
        Providers that do not implement this raise ``NotImplementedError``
        and the router automatically falls back to "faux streaming" — i.e.
        await ``complete()`` and replay the result as SSE chunks.

        Implementation note: the unreachable ``yield`` makes this an async
        generator so callers can safely do ``aiter = provider.complete_stream(...)``;
        the ``NotImplementedError`` then surfaces on the first ``__anext__()``.
        """
        raise NotImplementedError(
            f"{self.name!r} does not implement native SSE streaming"
        )
        yield  # pragma: no cover  — makes this an async generator

    def supports_model(self, model: str) -> bool:
        """Return True if this provider supports the given model ID."""
        return model in self.models

    def estimate_tokens(self, messages: List[Message]) -> int:
        """Estimate the token count for a list of messages using a simple word-count heuristic."""
        total = 0
        for m in messages:
            if isinstance(m.content, str):
                total += int(len(m.content.split()) * 1.3)
            elif isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and "text" in part:
                        total += int(len(part["text"].split()) * 1.3)
        return total

    # ------------------------------------------------------------------
    # Optional: dynamic model discovery
    # ------------------------------------------------------------------
    async def fetch_models(self, api_key: str) -> list[dict]:
        """
        Optional — fetch the provider's live model catalogue.

        The default implementation honours ``models_discovery_url`` if set
        (standard OpenAI-style ``GET /v1/models`` with Bearer auth).  Providers
        with non-standard catalog endpoints (Cohere, Cloudflare, Gemini, …)
        should override this method.

        Return shape::
            [{"id": str, "context": int | None, "free": bool | None}, ...]

        Raises:
            NotImplementedError — if the provider has no discovery endpoint.
            RateLimitError      — on 429.
            ProviderError       — on any other failure.
        """
        if not self.models_discovery_url:
            raise NotImplementedError(
                f"Provider {self.name!r} does not support dynamic model discovery"
            )
        # Validate scheme to prevent SSRF against internal services.
        from urllib.parse import urlparse
        _parsed = urlparse(self.models_discovery_url)
        if _parsed.scheme not in ("http", "https"):
            raise ProviderError(
                f"[{self.name}] models_discovery_url has an invalid scheme "
                f"{_parsed.scheme!r} — only http/https are allowed"
            )
        # Local imports keep the module dependency-light for tests that
        # subclass BaseProvider without httpx installed.
        import httpx
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept":        "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(self.models_discovery_url, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderError(
                f"[{self.name}] models fetch network error: {exc}"
            ) from exc

        if resp.status_code == 429:
            raise RateLimitError(f"[{self.name}] models fetch 429")
        if resp.status_code != 200:
            raise ProviderError(
                f"[{self.name}] models fetch {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError(
                f"[{self.name}] models response parse error: {exc}"
            ) from exc

        raw = data.get("data") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise ProviderError(
                f"[{self.name}] models response shape unexpected"
            )

        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("model") or item.get("name")
            if not mid:
                continue
            ctx = (item.get("context_length")
                   or item.get("context_window")
                   or item.get("context")
                   or item.get("max_context_length")
                   or None)
            try:
                ctx = int(ctx) if ctx is not None else None
            except (TypeError, ValueError):
                ctx = None
            # Best-effort free-tier flag from common fields.
            is_free: bool | None = None
            pricing = item.get("pricing")
            if isinstance(pricing, dict):
                try:
                    p = float(pricing.get("prompt", 0) or 0)
                    c = float(pricing.get("completion", 0) or 0)
                    is_free = (p == 0 and c == 0)
                except (TypeError, ValueError):
                    pass
            mid_str = str(mid)
            if is_free is None and mid_str.endswith(":free"):
                is_free = True
            out.append({"id": mid_str, "context": ctx, "free": is_free})
        return out
