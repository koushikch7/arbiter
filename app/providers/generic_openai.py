"""
Generic OpenAI-compatible provider adapter.

Each instance represents a user-configured custom provider from the UI.
Configuration is provided at construction time (not via class attributes)
so multiple instances can coexist with different base URLs, auth schemes,
and model lists.

Supports two auth schemes:
  - "bearer"      → ``Authorization: Bearer <key>`` (OpenAI, DeepSeek, …)
  - "anthropic"   → ``x-api-key: <key>`` + ``anthropic-version`` header
                    (Anthropic Messages API — request/response translated)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from app.providers.base import BaseProvider, RateLimitError, ProviderError
from app.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger(__name__)


class GenericOpenAIProvider(BaseProvider):
    """
    Instance-configured OpenAI-compatible provider. Routing system looks up
    ``self.name`` so each custom provider must have a unique, slugified name.
    """

    def __init__(
        self,
        *,
        name: str,
        label: str,
        base_url: str,
        auth_scheme: str = "bearer",
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        extra_headers: dict[str, str] | None = None,
        models: list[str] | None = None,
        max_context: int = 131_072,
        supports_discovery: bool = True,
    ):
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"Custom provider name must be alphanumeric (with _/-): {name!r}"
            )
        self.name = name
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.auth_scheme = auth_scheme
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.extra_headers = dict(extra_headers or {})
        self.models = list(models or [])
        self.max_context_tokens = max_context
        self.default_model = self.models[0] if self.models else ""
        self._supports_discovery = supports_discovery

    # ------------------------------------------------------------------
    def _build_headers(self, api_key: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent":   "Arbiter/1.11.2",
        }
        headers[self.auth_header] = f"{self.auth_prefix}{api_key}"
        headers.update(self.extra_headers)
        return headers

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        if self.auth_scheme == "anthropic":
            return await self._complete_anthropic(request, api_key)
        return await self._complete_openai(request, api_key)

    # -- OpenAI-compatible path ----------------------------------------
    async def _complete_openai(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        model = request.model or self.default_model
        if not model:
            raise ProviderError(f"Custom provider {self.name!r} has no default model")

        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict[str, Any] = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
            "top_p":       request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        url = f"{self.base_url}/chat/completions"
        headers = self._build_headers(api_key)

        logger.debug("[custom:%s] POST %s model=%s", self.name, url, model)

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(
                    f"[custom:{self.name}] network error: {exc}"
                ) from exc

        if resp.status_code == 429:
            raise RateLimitError(f"[custom:{self.name}] 429: {resp.text[:300]}")
        if resp.status_code == 402:
            raise RateLimitError(
                f"[custom:{self.name}] 402 (quota exhausted): {resp.text[:300]}"
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"[custom:{self.name}] {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        if "error" in data and not data.get("choices"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(f"[custom:{self.name}] error: {msg}")

        try:
            choice  = data["choices"][0]
            msg     = choice["message"]
            finish  = choice.get("finish_reason", "stop")
            usage_r = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"[custom:{self.name}] response parse error: {exc}"
            ) from exc

        pt = usage_r.get("prompt_tokens", 0)
        ct = usage_r.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id=data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object="chat.completion",
            created=data.get("created", int(time.time())),
            model=data.get("model", model),
            choices=[Choice(
                index=0,
                message=Message(role="assistant", content=msg.get("content", "")),
                finish_reason=finish,
            )],
            usage=Usage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
            ),
        )

    # -- Anthropic Messages API path -----------------------------------
    async def _complete_anthropic(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        model = request.model or self.default_model
        if not model:
            raise ProviderError(f"Custom provider {self.name!r} has no default model")

        # Anthropic expects system prompt as top-level `system`, messages array
        # to contain only user/assistant roles.
        system_parts: list[str] = []
        messages: list[dict] = []
        for m in request.messages:
            if m.role == "system":
                if isinstance(m.content, str):
                    system_parts.append(m.content)
            else:
                messages.append({"role": m.role, "content": m.content})

        payload: dict[str, Any] = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  request.max_tokens or 4096,  # required by Anthropic
            "temperature": request.temperature,
            "top_p":       request.top_p,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if request.stop:
            payload["stop_sequences"] = request.stop

        url = f"{self.base_url}/messages"
        headers = self._build_headers(api_key)

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(
                    f"[custom:{self.name}] anthropic network error: {exc}"
                ) from exc

        if resp.status_code == 429:
            raise RateLimitError(f"[custom:{self.name}] 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(
                f"[custom:{self.name}] anthropic {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        try:
            # Anthropic returns {"content": [{"type": "text", "text": "..."}], ...}
            content_blocks = data.get("content", [])
            text = "".join(
                b.get("text", "") for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            usage_r = data.get("usage", {})
            pt = usage_r.get("input_tokens", 0)
            ct = usage_r.get("output_tokens", 0)
            stop_reason = data.get("stop_reason", "stop")
            # Map Anthropic stop_reason to OpenAI finish_reason
            finish_map = {
                "end_turn": "stop", "max_tokens": "length",
                "stop_sequence": "stop", "tool_use": "tool_calls",
            }
            finish = finish_map.get(stop_reason, "stop")
        except Exception as exc:
            raise ProviderError(
                f"[custom:{self.name}] anthropic response parse error: {exc}"
            ) from exc

        return ChatCompletionResponse(
            id=data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object="chat.completion",
            created=int(time.time()),
            model=data.get("model", model),
            choices=[Choice(
                index=0,
                message=Message(role="assistant", content=text),
                finish_reason=finish,
            )],
            usage=Usage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
            ),
        )

    # ------------------------------------------------------------------
    async def fetch_models(self, api_key: str) -> list[dict]:
        if not self._supports_discovery:
            raise NotImplementedError(
                f"Custom provider {self.name!r} does not expose /v1/models"
            )

        url = f"{self.base_url}/models"
        headers = self._build_headers(api_key)

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(
                    f"[custom:{self.name}] models fetch network error: {exc}"
                ) from exc

        if resp.status_code == 429:
            raise RateLimitError(f"[custom:{self.name}] models fetch 429")
        if resp.status_code != 200:
            raise ProviderError(
                f"[custom:{self.name}] models fetch {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError(
                f"[custom:{self.name}] models response parse error: {exc}"
            ) from exc

        raw = data.get("data") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise ProviderError(
                f"[custom:{self.name}] models response shape unexpected"
            )

        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("model")
            if not mid:
                continue
            is_free: bool | None = None
            pricing = item.get("pricing")
            if isinstance(pricing, dict):
                try:
                    p = float(pricing.get("prompt", 0) or 0)
                    c = float(pricing.get("completion", 0) or 0)
                    is_free = p == 0 and c == 0
                except (TypeError, ValueError):
                    pass
            ctx = item.get("context_length") or item.get("context") or None
            try:
                ctx = int(ctx) if ctx is not None else None
            except (TypeError, ValueError):
                ctx = None
            out.append({"id": str(mid), "context": ctx, "free": is_free})

        return out
