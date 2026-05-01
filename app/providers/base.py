from abc import ABC, abstractmethod
from typing import List, Optional
from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, Message


class RateLimitError(Exception):
    """Raised when an API key hits its rate limit."""
    pass


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
