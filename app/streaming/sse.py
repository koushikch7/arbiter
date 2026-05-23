"""
SSE helpers for OpenAI-compatible chat.completion.chunk streaming.

Phase 1 strategy — universal "graceful streaming":
    1. Run ``provider.complete()`` exactly as in the non-streaming path.
    2. While awaiting it, emit SSE comment heartbeats (``: thinking\\n\\n``)
       every few seconds so reverse proxies (nginx, Cloudflare) keep the
       connection warm and clients see the request is still alive.
    3. Once the response arrives, replay it as a sequence of OpenAI-format
       ``chat.completion.chunk`` deltas in word-bursts, then ``data: [DONE]``.

Trade-off: TTFT (time to first token) is unchanged versus non-stream — the
client just sees the response in pieces instead of one blob. Real per-provider
SSE for low TTFT is a Phase 2 feature.

Cache hits get the same treatment, so a cached response replays as a stream
in milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Awaitable, Optional

from app.models.schemas import ChatCompletionResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL_S = 5.0   # SSE comment ping while awaiting upstream
CHUNK_BURST_DELAY_S  = 0.015 # delay between word-bursts during replay
CHUNK_WORDS          = 4     # words per delta chunk
SSE_DONE             = b"data: [DONE]\n\n"

# Status messages cycled in the SSE comments — invisible to OpenAI SDKs
# (they're SSE comments, ignored by EventSource), but visible to anyone
# reading the raw stream (curl, Arbiter playground).
_STATUS_MESSAGES = (
    "thinking",
    "evaluating",
    "generating",
    "almost there",
)


# ---------------------------------------------------------------------------
# Low-level SSE primitives
# ---------------------------------------------------------------------------
def sse_data(payload: dict) -> bytes:
    """Encode one ``data:`` SSE event."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def sse_comment(text: str) -> bytes:
    """Encode one SSE comment (ignored by clients, used for keepalive)."""
    # Strip newlines defensively so a comment can't accidentally terminate the event.
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n".encode("utf-8")


def sse_error(message: str, *, error_type: str = "provider_error", code: int = 502) -> bytes:
    """Encode an OpenAI-style error event."""
    return sse_data({
        "error": {
            "message": message,
            "type":    error_type,
            "code":    code,
        }
    })


# ---------------------------------------------------------------------------
# OpenAI chat.completion.chunk envelope
# ---------------------------------------------------------------------------
def _chunk_envelope(
    *,
    chunk_id: str,
    model: str,
    role: Optional[str] = None,
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[dict] = None,
) -> dict:
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    out: dict = {
        "id":      chunk_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        out["usage"] = usage
    return out


# ---------------------------------------------------------------------------
# Faux streaming — replay a finished response as SSE chunks
# ---------------------------------------------------------------------------
async def faux_stream_response(
    response: ChatCompletionResponse,
    *,
    model_name: str,
    arbiter_provider: Optional[str] = None,
) -> AsyncIterator[bytes]:
    """
    Replay a complete ``ChatCompletionResponse`` as an SSE chunk sequence.

    Emits:
      1. Initial chunk with ``delta.role = "assistant"``.
      2. N word-burst chunks with ``delta.content = ...``.
      3. Final chunk with ``finish_reason`` and (when known) ``usage``.
      4. Terminal ``data: [DONE]`` marker.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Surface the chosen provider/model as an SSE comment so curious clients
    # (e.g. Arbiter playground) can identify the upstream. OpenAI SDKs ignore it.
    if arbiter_provider:
        yield sse_comment(f"arbiter-model-used: {arbiter_provider}/{model_name}")

    yield sse_data(_chunk_envelope(chunk_id=chunk_id, model=model_name, role="assistant"))

    # Extract text + finish_reason
    text = ""
    finish_reason = "stop"
    if response.choices:
        ch = response.choices[0]
        if ch.message and ch.message.content is not None:
            content = ch.message.content
            text = content if isinstance(content, str) else json.dumps(content)
        finish_reason = ch.finish_reason or "stop"

    if text:
        # Split preserving spaces so concatenated chunks reproduce the original.
        words = text.split(" ")
        for i in range(0, len(words), CHUNK_WORDS):
            burst = " ".join(words[i:i + CHUNK_WORDS])
            # Re-add the trailing space between bursts (lost by split)
            if i + CHUNK_WORDS < len(words):
                burst += " "
            yield sse_data(_chunk_envelope(chunk_id=chunk_id, model=model_name, content=burst))
            await asyncio.sleep(CHUNK_BURST_DELAY_S)

    usage_dict = None
    if response.usage:
        usage_dict = {
            "prompt_tokens":     response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens":      response.usage.total_tokens,
        }

    yield sse_data(_chunk_envelope(
        chunk_id=chunk_id, model=model_name,
        finish_reason=finish_reason, usage=usage_dict,
    ))
    yield SSE_DONE


# ---------------------------------------------------------------------------
# Heartbeat orchestration
# ---------------------------------------------------------------------------
async def heartbeat_while_awaiting(
    awaitable: Awaitable,
    *,
    interval: float = HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[bytes]:
    """
    Async-iterate SSE heartbeats while ``awaitable`` runs.

    Yields ``sse_comment(...)`` events on a timer. When the awaitable
    completes, the iteration ends. The result (or exception) is *not*
    surfaced through the iterator — call ``task.result()`` on the
    Task you passed in (use the convenience wrapper below instead).
    """
    if not asyncio.isfuture(awaitable) and not isinstance(awaitable, asyncio.Task):
        task = asyncio.ensure_future(awaitable)
    else:
        task = awaitable  # type: ignore[assignment]

    i = 0
    try:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except asyncio.TimeoutError:
                yield sse_comment(_STATUS_MESSAGES[i % len(_STATUS_MESSAGES)])
                i += 1
            except Exception:
                # The task raised — return so caller can inspect via task.result()
                return
    except asyncio.CancelledError:
        task.cancel()
        raise


def status_message(index: int) -> str:
    """Return a cycling status string (exposed for tests / playground)."""
    return _STATUS_MESSAGES[index % len(_STATUS_MESSAGES)]
