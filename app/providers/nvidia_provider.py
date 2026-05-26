"""
NVIDIA NIM provider adapter (OpenAI-compatible endpoint).

NVIDIA NIM at build.nvidia.com provides an OpenAI-compatible API for LLMs,
accessible via API keys from https://build.nvidia.com.

API endpoint: https://integrate.api.nvidia.com/v1/chat/completions
Auth:         Authorization: Bearer nvapi-...
Models list:  https://integrate.api.nvidia.com/v1/models

Free tier: 1000 requests/day for most models (subject to change).
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

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1/chat/completions"


class NvidiaProvider(BaseProvider):
    name = "nvidia"
    models_discovery_url = "https://integrate.api.nvidia.com/v1/models"

    models: List[str] = [
        "nvidia/nemotron-3-super-120b-a12b",       # NVIDIA flagship MoE
        "meta/llama-3.3-70b-instruct",             # Meta Llama 70B
        "mistralai/mistral-medium-3.5-128b",       # Mistral flagship
        "mistralai/mistral-small-4-119b-2603",     # Mistral hybrid MoE
        "google/gemma-3-27b-it",                   # Gemma 3 (verified working)
    ]

    max_context_tokens = 131_072
    default_model      = "nvidia/nemotron-3-super-120b-a12b"

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

        # Forward OpenAI-compatible extras verbatim.
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

        logger.debug(f"NvidiaProvider POST model={model}")

        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                resp = await client.post(NVIDIA_API_BASE, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    f"NVIDIA upstream timeout after 45s for model {model!r} "
                    f"(model may be overloaded or unavailable)"
                ) from exc
            except httpx.RequestError as exc:
                raise ProviderError(f"NVIDIA network error: {exc!r}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"NVIDIA 429: {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
        if resp.status_code != 200:
            raise ProviderError(f"NVIDIA {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"NVIDIA response parse error: {exc}") from exc

        prompt_tokens     = usage_raw.get("prompt_tokens",     0)
        completion_tokens = usage_raw.get("completion_tokens", 0)

        # Build assistant message preserving extras (tool_calls, refusal, etc.)
        msg_kwargs: dict = {
            "role":    msg.get("role", "assistant"),
            "content": msg.get("content") or "",
        }
        for extra_key in ("tool_calls", "function_call", "refusal", "audio", "name"):
            val = msg.get(extra_key)
            # Skip None, empty lists, and empty strings — keep responses clean
            if val is not None and val != [] and val != "":
                msg_kwargs[extra_key] = val

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
        """Native SSE streaming for NVIDIA NIM."""
        from app.streaming.openai_stream import stream_openai_chat
        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
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
        try:
            extras = request.model_dump(exclude_unset=True)
        except Exception:
            extras = {}
        for k in ("tools", "tool_choice", "parallel_tool_calls", "response_format",
                  "seed", "logprobs", "top_logprobs", "user", "n", "presence_penalty",
                  "frequency_penalty", "logit_bias"):
            if k in extras and extras[k] is not None:
                payload[k] = extras[k]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async for chunk in stream_openai_chat(
            url=NVIDIA_API_BASE, headers=headers, payload=payload,
            provider_name="NVIDIA", timeout=120.0,
        ):
            yield chunk
