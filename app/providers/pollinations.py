"""
Pollinations.ai text provider adapter (OpenAI-compatible endpoint).

Requires an API key from https://enter.pollinations.ai/
(free tier available; keys start with sk_ or pk_).

Available models (March 2026):
  openai              GPT-based — recommended default
  openai-fast         Faster/cheaper GPT variant
  openai-large        Higher quality GPT variant
  claude              Claude-based backend
  claude-fast         Faster Claude variant
  claude-large        Higher quality Claude variant
  gemini              Gemini-based backend
  gemini-fast         Faster Gemini variant
  mistral             Mistral backend
  deepseek            DeepSeek backend
  qwen-coder          Qwen coding model

Endpoint:  POST https://gen.pollinations.ai/v1/chat/completions
Auth:      Bearer <api_key>

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

POLLINATIONS_API_BASE = "https://gen.pollinations.ai/v1/chat/completions"


class PollinationsProvider(BaseProvider):
    name = "pollinations"

    models: List[str] = [
        # Verified live April 2026 — legacy aliases (`mistral`, `mistral-large`,
        # `openai`, `claude`) were deprecated for authenticated users; only
        # `openai-fast` (GPT-OSS 20B on OVH) is reliably available.
        "openai-fast",
    ]

    max_context_tokens = 32768
    default_model      = "openai-fast"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Call the Pollinations.ai OpenAI-compatible text endpoint.

        No Authorization header is sent (free, anonymous service).  Unknown
        models are passed through so users get a clear upstream 4xx rather
        than silently being rerouted to a different model.
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
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        # Note: intentionally NO Authorization header — Pollinations is free / anonymous.
        # User-Agent required: Cloudflare in front of Pollinations returns 502 to
        # unrecognised UAs (e.g. bare httpx/0.x).
        headers = {
            "Content-Type": "application/json",
            "User-Agent":   "Arbiter/1.11.2 (+https://github.com/)",
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
