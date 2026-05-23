"""
Routeway provider adapter (OpenAI-compatible endpoint).

Routeway (https://routeway.ai) is a unified API gateway exposing models
from OpenAI, Anthropic, DeepSeek, and more behind a single endpoint at
https://api.routeway.ai/v1. Docs: https://docs.routeway.ai

Key features leveraged here:
  - OpenAI-compatible `POST /v1/chat/completions`
  - OpenAI-compatible `GET  /v1/models` (used by fetch_models())
  - Mix of free and paid models (routing priority left to upstream quota;
    quota-exhaustion is the natural backstop for paid models)
  - `Authorization: Bearer <api-key>` auth scheme

Rate limits are not publicly documented — conservative sentinel values are
configured in ``app/key_management/key_pool.py::PROVIDER_LIMITS``.
"""

import logging
import time
import uuid
from typing import List

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

ROUTEWAY_API_BASE     = "https://api.routeway.ai/v1"
ROUTEWAY_CHAT_URL     = f"{ROUTEWAY_API_BASE}/chat/completions"
ROUTEWAY_MODELS_URL   = f"{ROUTEWAY_API_BASE}/models"


class RoutewayProvider(BaseProvider):
    name = "routeway"

    # Curated starter list — **free-tier first**.  Routeway tags its zero-cost
    # models with a `:free` suffix (verified via /v1/models pricing endpoint,
    # 15 free models available as of April 2026).  The full 192-model catalogue
    # is fetched dynamically via `fetch_models()` from the UI's "Refresh Models"
    # button and merged into the state store.  This seed list ensures the
    # provider is usable out of the box without a manual refresh and keeps the
    # Arbiter "free-models-first" strategy consistent across providers.
    models: List[str] = [
        # ── Free tier (verified live April 2026, 9 reliably-working) ──
        "llama-3.3-70b-instruct:free",
        "devstral-2512:free",
        "ling-2.6-flash:free",
        "step-3.5-flash:free",
        "nemotron-nano-9b-v2:free",
        "llama-3.1-8b-instruct:free",
        "llama-3.2-3b-instruct:free",
        "llama-3.2-1b-instruct:free",
        "mistral-nemo-instruct:free",
        # ── Paid fallback (used only on explicit opt-in) ──
        "gpt-4o-mini",
        "gpt-4o",
        "claude-3-5-sonnet",
        "claude-3-haiku",
        "deepseek-chat",
        "deepseek-coder",
        "llama-3.3-70b",
    ]

    max_context_tokens = 262_144
    default_model      = "llama-3.3-70b-instruct:free"

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        # Allow unknown models to pass through — Routeway exposes many models
        # not in our seed list, and upstream will return a clear 400 if the
        # model ID is actually invalid.
        model = request.model or self.default_model

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
            "top_p":       request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    "Arbiter/1.11.2",
        }

        logger.debug(f"RoutewayProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(
                    ROUTEWAY_CHAT_URL, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                raise ProviderError(f"Routeway network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Routeway 429: {resp.text[:300]}")
        if resp.status_code == 402:
            # "Payment required" — out of credits on a paid model
            raise RateLimitError(f"Routeway 402 (quota exhausted): {resp.text[:300]}")
        if resp.status_code == 503:
            # 503 is MODEL-level ("No eligible providers" / upstream bad
            # gateway) not KEY-level. Raising ProviderError lets the router
            # move to the next model in the hierarchy WITHOUT putting the
            # key on cooldown (which would then cascade all other :free
            # models into 503s until the cooldown expires).
            raise ProviderError(f"Routeway 503 (model unavailable): {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Routeway {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Some OpenAI-compatible gateways return a 200 with an error body
        if "error" in data and not data.get("choices"):
            err = data["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            code = err.get("code", 0) if isinstance(err, dict) else 0
            if code in (429, 402):
                raise RateLimitError(f"Routeway error {code}: {msg}")
            # code == 503 or any other model-level failure → ProviderError
            # so we fall through to next model instead of burning the key.
            raise ProviderError(f"Routeway error {code or '?'}: {msg}")

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Routeway response parse error: {exc}") from exc

        prompt_tokens     = usage_raw.get("prompt_tokens",     0)
        completion_tokens = usage_raw.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id      = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object  = "chat.completion",
            created = data.get("created", int(time.time())),
            model   = data.get("model", model),
            choices = [
                Choice(
                    index         = 0,
                    message       = Message(role="assistant", content=msg.get("content", "")),
                    finish_reason = finish,
                )
            ],
            usage = Usage(
                prompt_tokens     = prompt_tokens,
                completion_tokens = completion_tokens,
                total_tokens      = prompt_tokens + completion_tokens,
            ),
        )

    # --------------------------------------------------------------------------
    async def fetch_models(self, api_key: str) -> list[dict]:
        """
        Fetch the live model catalogue from Routeway's `/v1/models` endpoint.

        Returns a list of dicts::

            [{"id": str, "context": int | None, "free": bool | None}, ...]

        Raises ProviderError on non-200; raises RateLimitError on 429.
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent":    "Arbiter/1.11.2",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(ROUTEWAY_MODELS_URL, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Routeway models fetch network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError("Routeway models fetch: rate limited")
        if resp.status_code != 200:
            raise ProviderError(
                f"Routeway models fetch {resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError(f"Routeway models response parse error: {exc}") from exc

        raw_list = data.get("data") if isinstance(data, dict) else data
        if not isinstance(raw_list, list):
            raise ProviderError(f"Routeway models response shape unexpected: {type(raw_list)}")

        out: list[dict] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("model")
            if not model_id:
                continue
            # Best-effort free detection — several OpenAI-compatible gateways
            # expose a `pricing` dict with `prompt` / `completion` costs.
            is_free: bool | None = None
            pricing = item.get("pricing")
            if isinstance(pricing, dict):
                try:
                    prompt_cost     = float(pricing.get("prompt", 0) or 0)
                    completion_cost = float(pricing.get("completion", 0) or 0)
                    is_free = prompt_cost == 0 and completion_cost == 0
                except (TypeError, ValueError):
                    is_free = None
            elif ":free" in str(model_id):
                is_free = True

            ctx = item.get("context_length") or item.get("context") or None
            try:
                ctx = int(ctx) if ctx is not None else None
            except (TypeError, ValueError):
                ctx = None

            out.append({"id": str(model_id), "context": ctx, "free": is_free})

        return out

    async def complete_stream(self, request: ChatCompletionRequest, api_key: str):
        """Native SSE streaming for Routeway."""
        from app.streaming.openai_stream import stream_openai_chat
        model = request.model or self.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict = {
            "model": model, "messages": messages,
            "temperature": request.temperature, "top_p": request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        payload.setdefault("stream_options", {"include_usage": True})
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async for chunk in stream_openai_chat(
            url=ROUTEWAY_CHAT_URL, headers=headers, payload=payload,
            provider_name="Routeway", timeout=90.0,
        ):
            yield chunk
