"""
Cohere provider adapter (Chat v2 API).

Current active free-tier models (March 2026):
  command-r7b-12-2024      128K ctx  – fastest, lightest 7B
  command-r-08-2024        128K ctx  – balanced R-series
  command-r-plus-08-2024   128K ctx  – highest quality R-series
  command-a-03-2025        256K ctx  – newest flagship (may be paid-only)

Deprecated (do NOT use – shut down September 15 2025):
  command-r, command-r-plus, command, command-light

Free-tier rate limits:
  RPM: 20 (Chat endpoint)
  Monthly cap: 1 000 API calls  ≈ 33 calls / day

Source: https://docs.cohere.com/docs/models
        https://docs.cohere.com/docs/rate-limits
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

COHERE_CHAT_URL = "https://api.cohere.ai/v2/chat"


class CohereProvider(BaseProvider):
    name = "cohere"

    models: List[str] = [
        "command-r7b-12-2024",     # fastest 7B, lowest quota cost
        "command-r-08-2024",       # balanced
        "command-r-plus-08-2024",  # highest quality
        "command-a-03-2025",       # newest flagship (256K ctx)
    ]

    max_context_tokens = 128_000
    default_model      = "command-r7b-12-2024"

    # --------------------------------------------------------------------------
    def _build_cohere_messages(self, messages: List[Message]) -> List[dict]:
        """
        Convert OpenAI-style messages to Cohere v2 format.

        Cohere v2 Chat API accepts system messages directly in the messages
        array as {"role": "system", "content": str}.  Do NOT send a top-level
        "system" field — that causes a 422 "unknown field" error.
        """
        cohere_msgs: List[dict] = []

        for msg in messages:
            role    = msg.role
            content = msg.content

            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )

            if role in ("system", "user", "assistant"):
                cohere_msgs.append({"role": role, "content": content})

        return cohere_msgs

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested

        cohere_msgs = self._build_cohere_messages(request.messages)

        payload: dict = {
            "model":       model,
            "messages":    cohere_msgs,
            "temperature": request.temperature,
            "p":           request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        logger.debug(f"CohereProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(COHERE_CHAT_URL, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Cohere network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Cohere 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Cohere {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Cohere v2 response schema
        try:
            # v2: data["message"]["content"][0]["text"]
            content_blocks = data["message"]["content"]
            text_content   = next(
                b["text"] for b in content_blocks if b.get("type") == "text"
            )
            finish_raw = data.get("finish_reason", "COMPLETE")
            finish     = "stop" if finish_raw in ("COMPLETE", "MAX_TOKENS") else finish_raw.lower()
        except (KeyError, IndexError, StopIteration) as exc:
            raise ProviderError(
                f"Cohere response parse error: {exc}. Body: {data}"
            ) from exc

        usage_raw         = data.get("usage", {})
        billed            = usage_raw.get("billed_units", {})
        prompt_tokens     = billed.get("input_tokens",  0)
        completion_tokens = billed.get("output_tokens", 0)

        return ChatCompletionResponse(
            id      = f"chatcmpl-{uuid.uuid4().hex[:8]}",
            object  = "chat.completion",
            created = int(time.time()),
            model   = model,
            choices = [
                Choice(
                    index         = 0,
                    message       = Message(role="assistant", content=text_content),
                    finish_reason = finish,
                )
            ],
            usage = Usage(
                prompt_tokens     = prompt_tokens,
                completion_tokens = completion_tokens,
                total_tokens      = prompt_tokens + completion_tokens,
            ),
        )
