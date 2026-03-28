"""
Google Gemini provider adapter.

Free-tier models – verified March 2026 (https://ai.google.dev/gemini-api/docs/models):

  Preview (free tier, frontier-class):
    gemini-3.1-flash-lite-preview  – 1M ctx, free tier  ← default (newest/fastest)
    gemini-3-flash-preview         – 1M ctx, free tier, frontier-class performance

  Stable (free tier):
    gemini-2.5-flash-lite          – 1M ctx, 15 RPM, 250K TPM, 1 000 RPD
    gemini-2.5-flash               – 1M ctx, 10 RPM, 250K TPM,   250 RPD

  Paid-only (NOT included – require billing):
    gemini-3.1-pro-preview         – paid only
    gemini-2.5-pro                 – paid only

Deprecated / shut-down (do NOT use):
  gemini-1.5-*  shut down September 24 2025
  gemini-2.0-*  deprecated, retiring June 1 2026
  gemini-pro    legacy / limited availability

Source: https://ai.google.dev/gemini-api/docs/models
        https://ai.google.dev/gemini-api/docs/rate-limits
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

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(BaseProvider):
    name = "gemini"

    # Free-tier models ordered: newest preview first → stable fallback
    # gemini-3.1-pro-preview and gemini-2.5-pro are PAID-only — excluded
    models: List[str] = [
        "gemini-3.1-flash-lite-preview",  # newest, free tier, frontier-class fast
        "gemini-3-flash-preview",         # free tier, frontier-class quality
        "gemini-2.5-flash-lite",          # stable, 15 RPM, 1 000 RPD (highest quota)
        "gemini-2.5-flash",               # stable, 10 RPM, 250 RPD
    ]

    max_context_tokens = 1_048_576   # 1 M tokens (all models)
    default_model      = "gemini-3.1-flash-lite-preview"

    # --------------------------------------------------------------------------
    def _map_messages(self, messages: List[Message]) -> tuple:
        """
        Convert OpenAI-format messages → (system_text | None, gemini_contents).

        Gemini roles: "user" | "model"  (not "assistant")
        System messages are extracted and sent as systemInstruction.
        """
        system_parts: List[str] = []
        contents: List[dict]    = []

        for msg in messages:
            role    = msg.role
            content = msg.content

            # Normalise multi-part content to plain text
            if isinstance(content, list):
                text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = content or ""

            if role == "system":
                system_parts.append(text)
            elif role == "user":
                contents.append({"role": "user",  "parts": [{"text": text}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})

        # If there are system messages but no user turns yet, fold them into
        # the first user turn so the conversation is valid.
        if system_parts and not contents:
            contents.append({
                "role": "user",
                "parts": [{"text": "System: " + "\n".join(system_parts)}],
            })
            system_parts = []

        return system_parts, contents

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        model = request.model if request.model in self.models else self.default_model
        url   = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"

        system_parts, contents = self._map_messages(request.messages)

        # Prepend system instruction as leading user turn when present
        if system_parts:
            system_text = "\n".join(system_parts)
            contents    = [{"role": "user", "parts": [{"text": f"System: {system_text}"}]}] + contents

        generation_config: dict = {
            "temperature": request.temperature,
            "topP":        request.top_p,
        }
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if request.stop:
            generation_config["stopSequences"] = (
                request.stop if isinstance(request.stop, list) else [request.stop]
            )

        payload: dict = {
            "contents":         contents,
            "generationConfig": generation_config,
        }

        logger.debug(f"GeminiProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(url, json=payload)
            except httpx.RequestError as exc:
                raise ProviderError(f"Gemini network error: {exc}") from exc

        # 429 = quota exhausted / rate-limited; 403 = invalid key / project quota
        if resp.status_code in (429, 403):
            raise RateLimitError(
                f"Gemini {resp.status_code}: {resp.text[:300]}"
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"Gemini {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()

        # Parse response
        try:
            candidate    = data["candidates"][0]
            text_content = candidate["content"]["parts"][0]["text"]
            finish_raw   = candidate.get("finishReason", "STOP")
            finish       = "stop" if finish_raw in ("STOP", "MAX_TOKENS") else finish_raw.lower()
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"Gemini response parse error: {exc}. Body: {data}"
            ) from exc

        usage_meta         = data.get("usageMetadata", {})
        prompt_tokens      = usage_meta.get("promptTokenCount",     0)
        completion_tokens  = usage_meta.get("candidatesTokenCount", 0)

        return ChatCompletionResponse(
            id      = f"chatcmpl-{uuid.uuid4().hex[:8]}",
            object  = "chat.completion",
            created = int(time.time()),
            model   = model,
            choices = [
                Choice(
                    index        = 0,
                    message      = Message(role="assistant", content=text_content),
                    finish_reason= finish,
                )
            ],
            usage = Usage(
                prompt_tokens     = prompt_tokens,
                completion_tokens = completion_tokens,
                total_tokens      = prompt_tokens + completion_tokens,
            ),
        )
