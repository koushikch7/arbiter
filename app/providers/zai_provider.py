"""
Z.ai (Zhipu AI) provider adapter — OpenAI-compatible Chat API.

Free-tier models (March 2026):
  glm-4.7-flash   128K ctx  – fast, free, general purpose (confirmed $0)
  glm-4.5-flash   128K ctx  – balanced, free (confirmed $0)
  glm-z1-flash     32K ctx  – flash reasoning, free

Note: zai-glm-4.7 is ALSO accessible via Cerebras (Cerebras hosts it on their
hardware).  Keeping both providers active is intentional — Cerebras limits and
Z.ai limits are independent, so together they provide higher total capacity:
  Cerebras:  30 RPM  (zai-glm-4.7)
  Z.ai:      ~10 RPM (glm-4.7-flash)
  Combined:  ~40 RPM for GLM-4.7 class models

API base:  https://api.z.ai/api/paas/v4
Auth:      Authorization: Bearer {api_key}

Rate limits:  Check your account dashboard at z.ai/manage-apikey/rate-limits
  RPM:   ~10  (concurrency-based; exact value varies per account)
  TPM:   no published limit
  Daily: no published limit (some models may have monthly call caps)
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

ZAI_CHAT_URL = "https://api.z.ai/api/paas/v4/chat/completions"


class ZaiProvider(BaseProvider):
    name = "zai"

    models: List[str] = [
        "glm-4.7-flash",   # free · 128K · fast general purpose
        "glm-4.5-flash",   # free · 128K · balanced
        "glm-z1-flash",    # free · 32K  · flash reasoning
    ]

    max_context_tokens = 128_000
    default_model      = "glm-4.7-flash"

    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        model = request.model if request.model in self.models else self.default_model

        messages = []
        for msg in request.messages:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            messages.append({"role": msg.role, "content": content})

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
            "top_p":       request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }

        logger.debug(f"ZaiProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(ZAI_CHAT_URL, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Z.ai network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Z.ai 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Z.ai {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice  = data["choices"][0]
            content = choice["message"]["content"]
            finish  = choice.get("finish_reason", "stop") or "stop"
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"Z.ai response parse error: {exc}. Body: {data}"
            ) from exc

        usage_raw         = data.get("usage", {})
        prompt_tokens     = usage_raw.get("prompt_tokens", 0)
        completion_tokens = usage_raw.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id      = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object  = "chat.completion",
            created = data.get("created", int(time.time())),
            model   = model,
            choices = [
                Choice(
                    index         = 0,
                    message       = Message(role="assistant", content=content),
                    finish_reason = finish,
                )
            ],
            usage = Usage(
                prompt_tokens     = prompt_tokens,
                completion_tokens = completion_tokens,
                total_tokens      = prompt_tokens + completion_tokens,
            ),
        )
