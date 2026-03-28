"""
Pollinations.ai text provider adapter (OpenAI-compatible endpoint).

Completely free, no API key required (anonymous / IP-rate-limited).
The key pool stores a dummy "free" key so the key-pool machinery works;
no Authorization header is sent.

Available models (March 2026):
  mistral         fast, general purpose  ← default
  mistral-large   higher quality
  openai          GPT-based backend
  claude          Claude-based backend

Endpoint:  POST https://text.pollinations.ai/openai
Rate limit: ~5 RPM per IP (enforced by Pollinations server-side)

Source: https://github.com/pollinations/pollinations?tab=readme-ov-file#text-generation-api
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

POLLINATIONS_API_BASE = "https://text.pollinations.ai/openai"


class PollinationsProvider(BaseProvider):
    name = "pollinations"

    models: List[str] = [
        "mistral",        # fast, general purpose
        "mistral-large",  # higher quality
        "openai",         # GPT-based
        "claude",         # Claude-based
    ]

    max_context_tokens = 32768
    default_model      = "mistral"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str  # api_key unused — no auth needed
    ) -> ChatCompletionResponse:
        """
        Call the Pollinations.ai OpenAI-compatible text endpoint.
        No Authorization header is sent (free, anonymous service).
        Falls back to default_model when the requested model is unknown.
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

        # Note: intentionally NO Authorization header — Pollinations is free / anonymous
        headers = {
            "Content-Type": "application/json",
        }

        logger.debug(f"PollinationsProvider POST model={model}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(POLLINATIONS_API_BASE, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Pollinations network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Pollinations 429 (IP rate-limited): {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Pollinations {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Pollinations response parse error: {exc}") from exc

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
