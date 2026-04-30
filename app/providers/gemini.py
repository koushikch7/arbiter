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

    # Free-tier models ordered by user-defined priority (newest+best first).
    # gemini-3.1-flash-lite-preview  – newest preview, free, fast        ← default
    # gemini-2.5-flash               – 10 RPM, 250K TPM,  250 RPD
    # gemini-2.5-flash-lite          – 15 RPM, 250K TPM, 1000 RPD
    # gemini-3-flash-preview         – frontier flash preview, free
    # gemini-2.0-flash               – 15 RPM,  1M  TPM, 1500 RPD
    # gemini-2.0-flash-lite          – 30 RPM,  1M  TPM, 1500 RPD
    # gemini-2.5-pro                  – paid only (premium reasoning)
    # gemini-3.1-pro-preview         – paid only (frontier reasoning)
    # gemini-3-pro-preview           – paid only (premium)
    models: List[str] = [
        "gemini-3.1-flash-lite-preview",  # 1st priority — newest free preview
        "gemini-2.5-flash",               # 2nd priority — quality bump
        "gemini-2.5-flash-lite",          # 3rd priority — highest free quota
        "gemini-3-flash-preview",         # backup — frontier flash (free)
        "gemini-2.0-flash",               # legacy backup — high quota
        "gemini-2.0-flash-lite",          # legacy backup — highest quota
        "gemini-2.5-pro",                 # paid only — reasoning / premium
        "gemini-3.1-pro-preview",         # paid only — frontier reasoning
        "gemini-3-pro-preview",           # paid only — premium
    ]

    # Models that REQUIRE a paid Google Cloud billing account.  The router
    # gates these to keys tagged `#paid` in .env (see config.get_key_tiers).
    paid_models: set = {
        "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-customtools",
        "gemini-3-pro-preview",
        "gemini-2.5-pro",
        "gemini-pro-latest",
    }

    max_context_tokens = 1_048_576   # 1 M tokens
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
    async def fetch_models(self, api_key: str) -> list[dict]:
        """Discover Gemini models via the native ``/v1beta/models`` endpoint.

        Only returns models that support ``generateContent`` (i.e. usable for
        chat completions).  Strips the ``models/`` URI prefix from every id.
        """
        import httpx

        from app.providers.base import RateLimitError, ProviderError

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={api_key}&pageSize=200"
        )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url)
        except httpx.RequestError as exc:
            raise ProviderError(f"[gemini] models fetch network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError("[gemini] models fetch 429")
        if resp.status_code != 200:
            raise ProviderError(
                f"[gemini] models fetch {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        out: list[dict] = []
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            mid = item.get("name", "")
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            if not mid:
                continue
            ctx = item.get("inputTokenLimit") or None
            try:
                ctx = int(ctx) if ctx is not None else None
            except (TypeError, ValueError):
                ctx = None
            # Paid-tier inference: anything in our `paid_models` set.
            is_free = mid not in self.paid_models
            out.append({"id": mid, "context": ctx, "free": is_free})
        return out

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:

        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested
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
