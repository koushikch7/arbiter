"""
Lightning.ai (LitAI) provider adapter — OpenAI-compatible endpoint.

Lightning.ai hosts natively-trained open-weight models on their own GPU cloud
and exposes them through an OpenAI-compatible REST API (LitAI).

Free / pricing tier
  • ~37M token welcome credit on signup (no credit card required).
  • After credits are consumed: pay-per-token at $0.09–$0.52 per million tokens
    depending on model size.

Natively hosted models (March 2026 snapshot)
  • lightning-ai/gpt-oss-120b  — 131K context — flagship 120B open-source model
  • lightning-ai/gpt-oss-20b   — 131K context — efficient 20B model
  • nvidia/nemotron-3-super     — 256K context — ultra-fast Nemotron 3 Super
  • deepseek/deepseek-v3.1      — 164K context — DeepSeek V3.1
  • meta/llama-3.3-70b          — 128K context — Llama 3.3 70B

API base: https://lightning.ai/api/v1/chat/completions
Auth:     Bearer token (LIGHTNING_API_KEYS env var)
Source:   https://lightning.ai/docs/litai/home
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

LIGHTNING_API_BASE = "https://lightning.ai/api/v1/chat/completions"


class LightningProvider(BaseProvider):
    name = "lightning"

    # Ordered best → smallest; context windows from Lightning.ai docs
    models: List[str] = [
        "nvidia/nemotron-3-super",    # 256K ctx — ultra-fast
        "lightning-ai/gpt-oss-120b",  # 131K ctx — flagship 120B
        "deepseek/deepseek-v3.1",     # 164K ctx — DeepSeek V3.1
        "lightning-ai/gpt-oss-20b",   # 131K ctx — efficient 20B
        "meta/llama-3.3-70b",         # 128K ctx — Llama 3.3 70B
    ]

    max_context_tokens = 256_000       # nemotron-3-super
    default_model      = "lightning-ai/gpt-oss-20b"

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested

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

        logger.debug(f"LightningProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(
                    LIGHTNING_API_BASE, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                raise ProviderError(f"Lightning.ai network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Lightning.ai 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Lightning.ai {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Guard against OpenAI-style error bodies (status 200 but error field)
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            if code in (429, 503):
                raise RateLimitError(f"Lightning.ai error {code}: {err.get('message','')}")
            raise ProviderError(f"Lightning.ai error: {err}")

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Lightning.ai response parse error: {exc}") from exc

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
