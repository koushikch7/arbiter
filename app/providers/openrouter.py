"""
OpenRouter provider adapter (OpenAI-compatible endpoint).

Free-tier (:free) models – March 2026 snapshot.
All have ":free" suffix; listed by context window desc, quality desc.

Rate limits (free account, no credits):
  RPM: 20   RPD: 50
Rate limits (account with $10+ credits purchased):
  RPM: 20   RPD: 1 000

Source: https://openrouter.ai/models?q=free
        https://openrouter.ai/docs/api/reference/limits
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

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(BaseProvider):
    name = "openrouter"
    models_discovery_url = "https://openrouter.ai/api/v1/models"

    # OpenRouter free :free models.  Free tier: 20 RPM, 50 RPD (no credits) /
    # 1000 RPD ($10+ credits).  Each :free routes to a different upstream
    # provider; some are chronically rate-limited.  Router treats OpenRouter
    # as last-resort by giving it a low priority weight.
    models: List[str] = [
        "google/gemma-3-27b-it:free",                       # 131K ctx — Google's Gemma 3 (less rate-limited)
        "qwen/qwen3-30b-a3b:free",                          # 128K ctx — Qwen 3 MoE
        "deepseek/deepseek-r1:free",                        # reasoning
        "mistralai/mistral-small-3.1-24b-instruct:free",    # 128K ctx — Mistral
        "meta-llama/llama-3.3-70b-instruct:free",           # 70B
        "google/gemma-3-12b-it:free",                       # 131K ctx — lighter Gemma
        "qwen/qwen3-4b:free",                               # 128K ctx — fast, small
        "meta-llama/llama-3.2-3b-instruct:free",            # 131K ctx — smallest/fastest
        "nousresearch/hermes-3-llama-3.1-405b:free",        # 131K ctx — largest (often 429)
    ]

    max_context_tokens = 131_072
    default_model      = "google/gemma-3-27b-it:free"

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
            "HTTP-Referer":  "https://github.com/arbiter-llm",
            "X-Title":       "Arbiter",
        }

        logger.debug(f"OpenRouterProvider POST model={model}")

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(
                    OPENROUTER_API_BASE, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                raise ProviderError(f"OpenRouter network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"OpenRouter 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Check for OpenRouter-specific error body (status 200 but error field)
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            if code in (429, 503):
                raise RateLimitError(f"OpenRouter error {code}: {err.get('message','')}")
            raise ProviderError(f"OpenRouter error: {err}")

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"OpenRouter response parse error: {exc}") from exc

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

    async def complete_stream(self, request: ChatCompletionRequest, api_key: str):
        """Native SSE streaming for OpenRouter."""
        from app.streaming.openai_stream import stream_openai_chat
        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict = {
            "model": model, "messages": messages,
            "temperature": request.temperature, "top_p": request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        # Ask OpenRouter to include usage in the final stream chunk
        payload.setdefault("stream_options", {"include_usage": True})
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/arbiter-llm",
            "X-Title":       "Arbiter",
        }
        async for chunk in stream_openai_chat(
            url=OPENROUTER_API_BASE, headers=headers, payload=payload,
            provider_name="OpenRouter", timeout=120.0,
        ):
            yield chunk
