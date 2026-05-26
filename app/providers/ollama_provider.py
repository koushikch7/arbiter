"""
Ollama Cloud provider adapter — OpenAI-compatible endpoint.

Ollama runs a hosted inference service at https://ollama.com that exposes a
small catalogue of large open-weight models through an OpenAI-compatible API.
A free personal API key is created at https://ollama.com/settings/keys and
grants access to all ``:cloud`` -tagged models without charge (subject to a
per-minute / per-day rate limit that Ollama enforces server-side).

Free cloud catalogue (verified April 2026)
  • gpt-oss:20b-cloud          —  20B MoE, fastest
  • gpt-oss:120b-cloud         — 120B MoE, flagship quality
  • deepseek-v3.1:671b-cloud   — DeepSeek V3.1, 671B MoE
  • qwen3-coder:480b-cloud     — Qwen3 Coder, 480B MoE (code specialist)
  • kimi-k2:1t-cloud           — Moonshot Kimi K2, 1T MoE, 256K ctx
  • glm-4.6:cloud              — Z.ai GLM 4.6
  • minimax-m2:cloud           — MiniMax M2

API base: POST https://ollama.com/v1/chat/completions
Auth:     Authorization: Bearer <OLLAMA_API_KEY>
Docs:     https://docs.ollama.com/cloud
"""

import logging
import time
import uuid
from typing import List

import httpx

from app.providers.base import BaseProvider, RateLimitError, ProviderError, parse_retry_after
from app.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger(__name__)

OLLAMA_CLOUD_API = "https://ollama.com/v1/chat/completions"


class OllamaProvider(BaseProvider):
    name = "ollama"

    # Free cloud-hosted models — ordered smallest/fastest → largest.
    # Context windows per Ollama's model pages.
    # Note: kimi-k2:1t-cloud removed — upstream returns 500 Internal Server
    # Error consistently (Ollama-side issue, not ours).
    models: List[str] = [
        "gpt-oss:20b-cloud",          # 131K ctx · fastest
        "glm-4.6:cloud",              # 128K ctx · Z-ai GLM 4.6
        "minimax-m2:cloud",           # 192K ctx · MiniMax M2
        "qwen3-coder:480b-cloud",     # 256K ctx · coding specialist
        "gpt-oss:120b-cloud",         # 131K ctx · flagship 120B
        "deepseek-v3.1:671b-cloud",   # 164K ctx · DeepSeek V3.1
    ]

    max_context_tokens = 262_144       # qwen3-coder
    default_model      = "gpt-oss:20b-cloud"

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        # Pass the caller-requested model through verbatim unless it's the
        # "auto" sentinel.  The router's _model_hierarchy is responsible for
        # choosing the best candidate when no model was explicitly selected.
        requested = (request.model or "").strip()
        model = requested if requested and requested.lower() != "auto" else self.default_model

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        # Forward tool-calling fields if present
        for k in ("tools", "tool_choice", "parallel_tool_calls", "response_format"):
            v = getattr(request, k, None)
            if v is not None:
                payload[k] = v

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    "Arbiter/1.11.2",
        }

        logger.debug("OllamaProvider POST model=%s", model)

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    OLLAMA_CLOUD_API, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                raise ProviderError(f"Ollama network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Ollama 429: {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
        if resp.status_code == 402:
            raise RateLimitError(f"Ollama 402 (quota exhausted): {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
        if resp.status_code == 503:
            # Upstream model-level issue — try next model, not next key
            raise ProviderError(f"Ollama 503: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Ollama {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Guard against OpenAI-style error bodies returned with HTTP 200
        if "error" in data and not data.get("choices"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else 0
            msg  = err.get("message") if isinstance(err, dict) else str(err)
            if code in (429, 402):
                raise RateLimitError(f"Ollama error {code}: {msg}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
            raise ProviderError(f"Ollama error: {msg}")

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Ollama response parse error: {exc}") from exc

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

    async def complete_stream(self, request: ChatCompletionRequest, api_key: str):
        """Native SSE streaming for Ollama Cloud."""
        from app.streaming.openai_stream import stream_openai_chat
        requested = (request.model or "").strip()
        model = requested if requested and requested.lower() != "auto" else self.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict = {
            "model": model, "messages": messages,
            "temperature": request.temperature,
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        # Forward tool-calling fields if present
        for k in ("tools", "tool_choice", "parallel_tool_calls", "response_format"):
            v = getattr(request, k, None)
            if v is not None:
                payload[k] = v
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async for chunk in stream_openai_chat(
            url=OLLAMA_CLOUD_API, headers=headers, payload=payload,
            provider_name="Ollama", timeout=120.0,
        ):
            yield chunk
