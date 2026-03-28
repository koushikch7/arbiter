"""
HuggingFace Inference API provider adapter (OpenAI-compatible router endpoint).

Free models that reliably support chat completions (March 2026):
  Qwen/Qwen2.5-7B-Instruct          — most reliable free model  ← default
  HuggingFaceH4/zephyr-7b-beta      — general purpose
  mistralai/Mistral-7B-Instruct-v0.3 — Mistral base
  google/gemma-2-2b-it               — Google Gemma 2B

Endpoint:  https://router.huggingface.co/v1/chat/completions
Auth:      Authorization: Bearer {HF_TOKEN}

Source: https://huggingface.co/docs/api-inference/en/tasks/chat-completion
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

HF_API_BASE = "https://router.huggingface.co/v1/chat/completions"


class HuggingFaceProvider(BaseProvider):
    name = "huggingface"

    models: List[str] = [
        "Qwen/Qwen2.5-7B-Instruct",              # most reliable free model
        "HuggingFaceH4/zephyr-7b-beta",           # general purpose
        "mistralai/Mistral-7B-Instruct-v0.3",     # Mistral base
        "google/gemma-2-2b-it",                   # Google Gemma 2B (smallest)
    ]

    max_context_tokens = 32768
    default_model      = "Qwen/Qwen2.5-7B-Instruct"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """
        Call the HuggingFace Inference Router OpenAI-compatible endpoint.
        Falls back to default_model when the requested model is not in the list.
        Raises ProviderError on 503 (model loading / unavailable).
        """
        model = request.model if request.model in self.models else self.default_model

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
            raise RateLimitError(f"HuggingFace 429: {resp.text[:300]}")
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
