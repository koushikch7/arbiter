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

    # Catalogue from https://gen.pollinations.ai/docs (Apr 2026).  Pollinations
    # routes each alias to a different upstream provider (OpenAI/Anthropic/
    # Google/etc.), so we get effectively-free access to several premium models.
    # Order roughly by speed (fast variants first, premium last).
    models: List[str] = [
        "openai-fast",          # GPT-OSS via OVH — fastest, default
        "mistral",              # Mistral Small
        "qwen-coder",           # Qwen Coder — coding
        "deepseek",             # DeepSeek V3 — reasoning/coding
        "gemini-flash-lite-3.1",# Gemini 3.1 Flash-Lite
        "openai",               # GPT default
        "claude-fast",          # Claude Haiku-class
        "kimi",                 # Kimi K2
        "glm",                  # GLM-4.x
        "nova-fast",            # Amazon Nova
        # Premium / large variants — used only when intent demands quality
        "openai-large",
        "claude",
        "claude-large",
        "claude-opus-4.7",
        "gemini-large",
        "deepseek-pro",
        "qwen-large",
        "qwen-coder-large",
        "mistral-large",
        "kimi-k2.6",
        "grok",
        "grok-large",
        "perplexity-reasoning",
        "perplexity-fast",
        "nova",
        "minimax",
    ]

    max_context_tokens = 128_000
    default_model      = "openai-fast"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Call the Pollinations.ai OpenAI-compatible text endpoint.

        Pollinations now requires Authorization: Bearer <key> on every
        /v1/chat/completions call (changed in 2026 — was previously open).
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

        # Authorization Bearer is required as of 2026.  User-Agent is also
        # required — Cloudflare in front of Pollinations returns 502 to
        # unrecognised UAs (e.g. bare httpx/0.x).
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent":    "Arbiter/1.13 (+https://github.com/koushikch7/arbiter)",
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
