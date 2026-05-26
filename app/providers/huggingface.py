"""
HuggingFace Inference API provider adapter (OpenAI-compatible router endpoint).

Verified chat-compatible models (May 2026) — via router.huggingface.co:
  openai/gpt-oss-20b:fastest          — default, fast small model
  openai/gpt-oss-120b:fastest         — large GPT-OSS reasoning
  deepseek-ai/DeepSeek-V3.1:fastest   — DeepSeek V3.1
  deepseek-ai/DeepSeek-R1:fastest     — DeepSeek R1 reasoning
  meta-llama/Llama-3.3-70B-Instruct:fastest — Llama 3.3 70B
  Qwen/Qwen3-32B:fastest              — Qwen 3 32B

NOTE: The following models were removed (May 2026) — HF router returns
  HTTP 400 "not a chat model" for these IDs:
    mistralai/Mistral-7B-Instruct-v0.3
    meta-llama/Llama-3.1-8B-Instruct
    Qwen/Qwen2.5-7B-Instruct

Endpoint:  https://router.huggingface.co/v1/chat/completions
Auth:      Authorization: Bearer {HF_TOKEN}

Source: https://huggingface.co/docs/api-inference/en/tasks/chat-completion
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

HF_API_BASE = "https://router.huggingface.co/v1/chat/completions"


class HuggingFaceProvider(BaseProvider):
    name = "huggingface"
    models_discovery_url = "https://router.huggingface.co/v1/models"

    # HuggingFace Inference Providers (May 2026) — `:fastest` suffix routes
    # to whichever backend (Cerebras/Together/Sambanova/Groq/Novita/etc.) has
    # lowest latency.  No markup over partner pricing.
    # NOTE: Mistral-7B-Instruct-v0.3, Llama-3.1-8B-Instruct, Qwen2.5-7B-Instruct
    # removed — HF router returns HTTP 400 "not a chat model" as of May 2026.
    models: List[str] = [
        "openai/gpt-oss-20b:fastest",                # default — fast small
        "openai/gpt-oss-120b:fastest",               # large GPT-OSS reasoning
        "deepseek-ai/DeepSeek-V3.1:fastest",         # DeepSeek V3.1
        "deepseek-ai/DeepSeek-R1:fastest",           # DeepSeek R1 reasoning
        "meta-llama/Llama-3.3-70B-Instruct:fastest", # Llama 3.3 70B
        "Qwen/Qwen3-32B:fastest",                    # Qwen 3 32B
    ]

    max_context_tokens = 131_072
    default_model      = "openai/gpt-oss-20b:fastest"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Call the HuggingFace Inference Router OpenAI-compatible endpoint.

        When the caller names a specific model, pass it through as-is so the
        user gets exactly what they asked for (or a clear 4xx from HF if the
        id is wrong).  Only empty/"auto" model names fall back to default.
        Raises ProviderError on 503 (model loading / unavailable).
        """
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
            "top_p":       request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }

        logger.debug(f"HuggingFaceProvider POST model={model}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(HF_API_BASE, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"HuggingFace network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"HuggingFace 429: {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
        if resp.status_code == 503:
            # Model is loading or temporarily unavailable
            raise ProviderError(f"HuggingFace 503 (model loading/unavailable): {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"HuggingFace {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"HuggingFace response parse error: {exc}") from exc

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
        """Native SSE streaming for HuggingFace Inference Router."""
        from app.streaming.openai_stream import stream_openai_chat
        requested = (request.model or "").strip()
        model = requested if requested and requested.lower() != "auto" else self.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict = {
            "model": model, "messages": messages,
            "temperature": request.temperature, "top_p": request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async for chunk in stream_openai_chat(
            url=HF_API_BASE, headers=headers, payload=payload,
            provider_name="HuggingFace", timeout=120.0,
        ):
            yield chunk
