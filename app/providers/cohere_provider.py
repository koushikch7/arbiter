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
from typing import List, Optional

import httpx

from app.providers.base import (
    BaseProvider, RateLimitError, ProviderError, parse_retry_after,
    get_shared_async_client,
)
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

    # From docs.cohere.com/docs/models (Apr 2026).  All Live; deprecated
    # `command-r`, `command-r-plus`, `command-r-03-2024`, `command-r-plus-04-2024`
    # were removed Sept 2025.
    models: List[str] = [
        "command-r7b-12-2024",         # default — fastest 7B (128K ctx)
        "command-r-08-2024",           # balanced (128K ctx)
        "command-r-plus-08-2024",      # high-quality (128K ctx)
        "command-a-03-2025",           # flagship (256K ctx, may need prod key)
        "command-a-reasoning-08-2025", # reasoning model
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

        client = get_shared_async_client()
        try:
            resp = await client.post(COHERE_CHAT_URL, json=payload, headers=headers, timeout=90.0)
        except httpx.RequestError as exc:
            raise ProviderError(f"Cohere network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Cohere 429: {resp.text[:300]}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
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

    # ------------------------------------------------------------------
    async def complete_stream(self, request: ChatCompletionRequest, api_key: str):
        """Native SSE streaming for Cohere v2 chat.

        Translates Cohere's typed event stream (``message-start``,
        ``content-delta``, ``message-end``) into OpenAI ``chat.completion.chunk``
        envelopes.
        """
        import json as _json
        import httpx as _httpx

        requested = (request.model or "").strip()
        model = self.default_model if (not requested or requested.lower() == "auto") else requested
        cohere_msgs = self._build_cohere_messages(request.messages)

        payload: dict = {
            "model":       model,
            "messages":    cohere_msgs,
            "temperature": request.temperature,
            "p":           request.top_p,
            "stream":      True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created  = int(time.time())
        role_emitted = False
        last_finish: Optional[str] = None
        last_usage: Optional[dict] = None

        try:
            async with _httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", COHERE_CHAT_URL, json=payload, headers=headers) as resp:
                    if resp.status_code == 429:
                        body = await resp.aread()
                        raise RateLimitError(f"Cohere 429: {body[:300].decode('utf-8','replace')}",
                retry_after=parse_retry_after(getattr(resp, "headers", None), getattr(resp, "text", "")))
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ProviderError(
                            f"Cohere {resp.status_code}: {body[:500].decode('utf-8','replace')}"
                        )

                    async for raw in resp.aiter_lines():
                        if not raw:
                            continue
                        # Cohere emits both bare JSON lines and SSE-style "data: ..." lines
                        if raw.startswith(":"):
                            continue
                        data_str = raw[5:].strip() if raw.startswith("data:") else raw.strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            ev = _json.loads(data_str)
                        except _json.JSONDecodeError:
                            continue

                        ev_type = ev.get("type", "")
                        if ev_type == "message-start":
                            if not role_emitted:
                                yield {
                                    "id":      chunk_id,
                                    "object":  "chat.completion.chunk",
                                    "created": created,
                                    "model":   model,
                                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                                }
                                role_emitted = True
                        elif ev_type == "content-delta":
                            try:
                                text_piece = ev["delta"]["message"]["content"]["text"]
                            except (KeyError, TypeError):
                                text_piece = ""
                            if text_piece:
                                if not role_emitted:
                                    yield {
                                        "id":      chunk_id,
                                        "object":  "chat.completion.chunk",
                                        "created": created,
                                        "model":   model,
                                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                                    }
                                    role_emitted = True
                                yield {
                                    "id":      chunk_id,
                                    "object":  "chat.completion.chunk",
                                    "created": created,
                                    "model":   model,
                                    "choices": [{"index": 0, "delta": {"content": text_piece}, "finish_reason": None}],
                                }
                        elif ev_type == "message-end":
                            delta = ev.get("delta") or {}
                            finish_raw = delta.get("finish_reason") or ev.get("finish_reason") or "COMPLETE"
                            last_finish = "stop" if finish_raw in ("COMPLETE", "MAX_TOKENS") else str(finish_raw).lower()
                            usage_raw = delta.get("usage") or {}
                            billed    = usage_raw.get("billed_units") or {}
                            if billed:
                                pt = int(billed.get("input_tokens",  0) or 0)
                                ct = int(billed.get("output_tokens", 0) or 0)
                                last_usage = {
                                    "prompt_tokens":     pt,
                                    "completion_tokens": ct,
                                    "total_tokens":      pt + ct,
                                }
        except _httpx.RequestError as exc:
            raise ProviderError(f"Cohere stream network error: {exc}") from exc

        final_chunk = {
            "id":      chunk_id,
            "object":  "chat.completion.chunk",
            "created": created,
            "model":   model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": last_finish or "stop"}],
        }
        if last_usage:
            final_chunk["usage"] = last_usage
        yield final_chunk
