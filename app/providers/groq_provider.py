"""
Groq provider adapter (OpenAI-compatible endpoint).

Current active free-tier models (March 2026):
  llama-3.1-8b-instant              131K ctx  30 RPM  6 000 TPM  14 400 RPD  ← default
  llama-3.3-70b-versatile           131K ctx  30 RPM 12 000 TPM   1 000 RPD
  meta-llama/llama-4-scout-17b-…    131K ctx  30 RPM 30 000 TPM   1 000 RPD  (preview)
  qwen/qwen3-32b                    131K ctx  60 RPM  6 000 TPM   1 000 RPD
  moonshotai/kimi-k2-instruct       131K ctx  60 RPM 10 000 TPM   1 000 RPD
  openai/gpt-oss-120b               131K ctx  30 RPM  8 000 TPM   1 000 RPD
  openai/gpt-oss-20b                131K ctx  30 RPM  8 000 TPM   1 000 RPD

Removed / not in active model list (do NOT use):
  llama3-8b-8192, llama3-70b-8192, mixtral-8x7b-32768, gemma2-9b-it

Source: https://console.groq.com/docs/models
        https://console.groq.com/docs/rate-limits
"""

import logging
import time
import uuid
from typing import List

import httpx

from app.providers.base import BaseProvider, RateLimitError, ProviderError, parse_retry_after
from app.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger(__name__)

GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(BaseProvider):
    name = "groq"
    models_discovery_url = "https://api.groq.com/openai/v1/models"

    models: List[str] = [
        "llama-3.1-8b-instant",                        # 30 RPM · 6K TPM · 14,400 RPD — fastest
        "llama-3.3-70b-versatile",                     # 30 RPM · 12K TPM · 1,000 RPD — best quality
        "meta-llama/llama-4-scout-17b-16e-instruct",   # 30 RPM · 30K TPM · 1,000 RPD — Llama 4
        "qwen/qwen3-32b",                               # 60 RPM · 6K TPM  · 1,000 RPD — high RPM
        "moonshotai/kimi-k2-instruct",                  # 60 RPM · 10K TPM · 1,000 RPD
        "moonshotai/kimi-k2-instruct-0905",             # 60 RPM · 10K TPM · 1,000 RPD (alt version)
        "openai/gpt-oss-120b",                          # 30 RPM · 8K TPM  · 1,000 RPD — large
        "openai/gpt-oss-20b",                           # 30 RPM · 8K TPM  · 1,000 RPD — small
    ]

    max_context_tokens = 131_072
    default_model      = "llama-3.1-8b-instant"

    # --------------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """
        Groq is OpenAI-compatible, so we pass the request through with minimal
        transformation.  Falls back to default_model when the requested model
        isn't in our active list.
        """
        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]
        # Preserve OpenAI extras on incoming messages (tool_calls, tool_call_id,
        # name, refusal, …) — necessary for tool-using multi-turn chats.
        for src, dst in zip(request.messages, messages):
            try:
                src_extra = src.model_dump(exclude_unset=True)
            except Exception:
                src_extra = {}
            for k in ("tool_calls", "tool_call_id", "name", "function_call", "refusal"):
                if k in src_extra and src_extra[k] is not None:
                    dst[k] = src_extra[k]

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

        # Forward OpenAI-compatible extras (tools, tool_choice, response_format,
        # parallel_tool_calls, seed, logprobs, user, …) verbatim. The schema
        # uses `extra="allow"` so unknown fields are stored on the model dump.
        _passthrough_keys = (
            "tools", "tool_choice", "parallel_tool_calls", "response_format",
            "seed", "logprobs", "top_logprobs", "user", "n", "presence_penalty",
            "frequency_penalty", "logit_bias",
        )
        try:
            extras = request.model_dump(exclude_unset=True)
        except Exception:
            extras = {}
        for k in _passthrough_keys:
            if k in extras and extras[k] is not None:
                payload[k] = extras[k]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }

        logger.debug(f"GroqProvider POST model={model}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(GROQ_API_BASE, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Groq network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Groq 429: {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
        if resp.status_code != 200:
            raise ProviderError(f"Groq {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        # Groq returns standard OpenAI-format JSON
        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Groq response parse error: {exc}") from exc

        prompt_tokens     = usage_raw.get("prompt_tokens",     0)
        completion_tokens = usage_raw.get("completion_tokens", 0)

        # Build assistant message preserving extras (tool_calls, refusal, etc.)
        msg_kwargs: dict = {
            "role":    msg.get("role", "assistant"),
            "content": msg.get("content") or "",
        }
        for extra_key in ("tool_calls", "function_call", "refusal", "audio", "name"):
            if extra_key in msg and msg[extra_key] is not None:
                msg_kwargs[extra_key] = msg[extra_key]

        return ChatCompletionResponse(
            id      = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object  = "chat.completion",
            created = data.get("created", int(time.time())),
            model   = data.get("model", model),
            choices = [
                Choice(
                    index         = 0,
                    message       = Message(**msg_kwargs),
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
        """Native SSE streaming for Groq."""
        from app.streaming.openai_stream import stream_openai_chat
        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        # Preserve OpenAI message extras (tool_calls, refusal, ...)
        for src, dst in zip(request.messages, messages):
            try:
                src_extra = src.model_dump(exclude_unset=True)
            except Exception:
                src_extra = {}
            for k in ("tool_calls", "tool_call_id", "name", "function_call", "refusal"):
                if k in src_extra and src_extra[k] is not None:
                    dst[k] = src_extra[k]
        payload: dict = {
            "model": model, "messages": messages,
            "temperature": request.temperature, "top_p": request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        # Forward OpenAI extras
        try:
            extras = request.model_dump(exclude_unset=True)
        except Exception:
            extras = {}
        for k in ("tools", "tool_choice", "parallel_tool_calls", "response_format",
                  "seed", "logprobs", "top_logprobs", "user", "n", "presence_penalty",
                  "frequency_penalty", "logit_bias"):
            if k in extras and extras[k] is not None:
                payload[k] = extras[k]
        # Ask Groq to include usage in the final stream chunk
        payload.setdefault("stream_options", {"include_usage": True})
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async for chunk in stream_openai_chat(
            url=GROQ_API_BASE, headers=headers, payload=payload,
            provider_name="Groq", timeout=60.0,
        ):
            yield chunk
