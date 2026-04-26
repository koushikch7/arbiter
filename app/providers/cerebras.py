"""
Cerebras Inference provider adapter (OpenAI-compatible endpoint).

Models verified March 2026 (https://inference-docs.cerebras.ai/introduction):

  Production:
    llama3.1-8b              30 RPM · 60K TPM · 1M tokens/day  ← default (fastest)
    gpt-oss-120b             30 RPM · 64K TPM · 1M tokens/day  (large GPT-OSS)

  Preview:
    qwen-3-235b-a22b-instruct-2507   Qwen 3 235B (large reasoning)
    zai-glm-4.7                       Z.ai GLM 4.7

Endpoint:  https://api.cerebras.ai/v1/chat/completions
Auth:      Authorization: Bearer {api_key}
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

CEREBRAS_API_BASE = "https://api.cerebras.ai/v1/chat/completions"


class CerebrasProvider(BaseProvider):
    name = "cerebras"

    models: List[str] = [
        "llama3.1-8b",                       # production · 30 RPM · 60K TPM · 1M/day · fastest
        "gpt-oss-120b",                      # production · 30 RPM · 64K TPM · 1M/day · large
        "qwen-3-235b-a22b-instruct-2507",    # preview · Qwen 3 235B · best reasoning
        "zai-glm-4.7",                       # preview · Z.ai GLM 4.7
    ]

    max_context_tokens = 8192
    default_model      = "llama3.1-8b"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """
        Send a chat completion request to the Cerebras OpenAI-compatible API.
        Falls back to default_model when the requested model is unknown.
        """
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

        logger.debug(f"CerebrasProvider POST model={model}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(CEREBRAS_API_BASE, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Cerebras network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Cerebras 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Cerebras {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Cerebras response parse error: {exc}") from exc

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
