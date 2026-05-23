"""
Shared native SSE streaming helper for OpenAI-compatible providers.

Most of Arbiter's providers (Groq, Cerebras, OpenRouter, Cloudflare, NVIDIA,
HuggingFace, Pollinations, Routeway, Ollama, Z.ai) all
expose a POST ``/chat/completions`` endpoint that, when called with
``"stream": true``, returns ``text/event-stream`` lines of the form::

    data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"..."}}]}
    data: {"id":"...","choices":[{"delta":{},"finish_reason":"stop"}]}
    data: [DONE]

This module factors out the HTTP + SSE-parsing logic so each provider's
``complete_stream()`` is ~10 lines: build the same payload/headers/url it
already builds for ``complete()``, set ``stream=True``, and ``yield from``
the helper.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

from app.providers.base import ProviderError, RateLimitError

logger = logging.getLogger(__name__)


async def stream_openai_chat(
    *,
    url: str,
    headers: dict,
    payload: dict,
    provider_name: str,
    timeout: float = 120.0,
    extra_query: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """
    POST ``payload`` to ``url`` with ``"stream": true`` and yield each
    ``chat.completion.chunk`` JSON object parsed from the SSE stream.

    Yields:
        dict — one parsed chunk envelope per ``data:`` line.

    Raises:
        RateLimitError — on HTTP 429.
        ProviderError  — on any other HTTP error or network failure.
    """
    payload = {**payload, "stream": True}

    # ``httpx.AsyncClient.stream()`` MUST be used inside ``async with`` so the
    # connection lives for the entire iteration. We yield from inside the
    # ``async with`` so chunks arrive as soon as the upstream sends them.
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers, params=extra_query
            ) as resp:
                if resp.status_code == 429:
                    body = await resp.aread()
                    raise RateLimitError(
                        f"{provider_name} 429: {body[:300].decode('utf-8', 'replace')}"
                    )
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise ProviderError(
                        f"{provider_name} {resp.status_code}: "
                        f"{body[:500].decode('utf-8', 'replace')}"
                    )

                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    # Some upstreams emit comment lines ("`: ping`"); ignore.
                    if raw_line.startswith(":"):
                        continue
                    if not raw_line.startswith("data:"):
                        continue
                    data = raw_line[5:].strip()
                    if data == "[DONE]":
                        return
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug(
                            f"[{provider_name}] skipping non-JSON SSE line: {data[:120]!r}"
                        )
                        continue
                    # Some providers wrap errors in chunks even on 200
                    if isinstance(chunk, dict) and "error" in chunk and "choices" not in chunk:
                        err = chunk["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        code = err.get("code", 0) if isinstance(err, dict) else 0
                        if code in (429, 503):
                            raise RateLimitError(f"{provider_name} stream error {code}: {msg}")
                        raise ProviderError(f"{provider_name} stream error: {msg}")
                    yield chunk
    except httpx.RequestError as exc:
        raise ProviderError(f"{provider_name} stream network error: {exc}") from exc


def extract_delta_content(chunk: dict) -> str:
    """Extract ``choices[0].delta.content`` from a chunk envelope; '' if absent."""
    try:
        delta = chunk["choices"][0].get("delta") or {}
        content = delta.get("content")
        return content if isinstance(content, str) else ""
    except (KeyError, IndexError, AttributeError, TypeError):
        return ""


def extract_finish_reason(chunk: dict) -> Optional[str]:
    """Return ``choices[0].finish_reason`` or None."""
    try:
        return chunk["choices"][0].get("finish_reason")
    except (KeyError, IndexError, AttributeError, TypeError):
        return None


def extract_usage(chunk: dict) -> Optional[dict]:
    """Return chunk-level usage dict if present (final chunk on most providers)."""
    u = chunk.get("usage") if isinstance(chunk, dict) else None
    if isinstance(u, dict):
        return u
    return None
