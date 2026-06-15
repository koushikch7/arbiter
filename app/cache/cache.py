import base64
import gzip
import hashlib
import json
import logging
from typing import Optional

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "arbiter:cache:"

# Magic prefix used to identify gzip-compressed cache payloads. The Redis
# client runs with ``decode_responses=True`` so we cannot store raw binary —
# we base64-encode the gzipped bytes to keep the value valid UTF-8. Entries
# without this prefix are read as plain JSON for backward compatibility.
_GZIP_MARKER = "GZ1:"
# Only compress payloads that actually benefit from it. Below ~512 bytes the
# gzip header + base64 overhead exceeds any savings.
_MIN_COMPRESS_BYTES = 512


def _maybe_compress(payload: str) -> str:
    """Compress *payload* if it is large enough to benefit, else return as-is."""
    raw = payload.encode("utf-8")
    if len(raw) < _MIN_COMPRESS_BYTES:
        return payload
    try:
        compressed = gzip.compress(raw, compresslevel=6)
        if len(compressed) >= len(raw):
            # Pathological — already incompressible
            return payload
        return _GZIP_MARKER + base64.b64encode(compressed).decode("ascii")
    except Exception:
        return payload


def _maybe_decompress(raw: str) -> str:
    """Inverse of :func:`_maybe_compress`. Tolerates legacy plain JSON values."""
    if not isinstance(raw, str) or not raw.startswith(_GZIP_MARKER):
        return raw
    try:
        b64 = raw[len(_GZIP_MARKER):]
        return gzip.decompress(base64.b64decode(b64)).decode("utf-8")
    except Exception as exc:
        logger.warning("Failed to decompress cache payload: %s", exc)
        raise


def _normalize_for_key(value):
    """Return a deterministic, JSON-serialisable form of *value* for hashing.

    Handles Pydantic models (``tools`` entries), nested lists/dicts, and
    primitives. Used by :meth:`CacheLayer.make_key` so structured fields hash
    consistently regardless of object identity.
    """
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(exclude_none=True)
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_for_key(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_for_key(v) for k, v in value.items()}
    return value



class CacheLayer:
    """Redis-backed cache for LLM responses."""

    def __init__(self, redis_client, default_ttl: int = 3600):
        self.redis = redis_client
        self.default_ttl = default_ttl

    def make_key(self, request: ChatCompletionRequest) -> str:
        """Build a deterministic cache key from the request parameters.

        Includes model, messages, max_tokens, stop sequences, and top_p so
        that requests with different output constraints never share a cache
        entry (otherwise a short-max_tokens response could be served as a
        full-length answer).

        Also includes the tool / structured-output controls — ``tools``,
        ``tool_choice``, ``response_format`` and ``seed`` (F3, v1.21.0). These
        change the model's output, so without them a tool-call or JSON-mode
        request could collide with — and be served — a plain-text answer that
        ignored them entirely.
        """
        messages_data = []
        for m in request.messages:
            messages_data.append({"role": m.role, "content": m.content})

        key_payload = json.dumps(
            {
                "model":           request.model,
                "messages":        messages_data,
                "max_tokens":      request.max_tokens,
                "stop":            request.stop,
                "top_p":           request.top_p,
                "tools":           _normalize_for_key(getattr(request, "tools", None)),
                "tool_choice":     _normalize_for_key(getattr(request, "tool_choice", None)),
                "response_format": _normalize_for_key(getattr(request, "response_format", None)),
                "seed":            getattr(request, "seed", None),
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(key_payload.encode()).hexdigest()
        return f"{CACHE_KEY_PREFIX}{digest}"

    def _should_cache(self, request: ChatCompletionRequest) -> bool:
        """Only cache requests with low temperature (deterministic enough)."""
        return request.temperature <= 0.3

    async def get(self, key: str) -> Optional[ChatCompletionResponse]:
        """Retrieve a cached response, or None if not found."""
        try:
            raw = await self.redis.get(key)
            if raw is None:
                return None
            raw = _maybe_decompress(raw)
            data = json.loads(raw)
            response = ChatCompletionResponse(**data)
            logger.debug(f"Cache HIT for key {key[-8:]}")
            return response
        except Exception as e:
            logger.warning(f"Cache get error for key {key[-8:]}: {e}")
            return None

    async def set(
        self, key: str, response: ChatCompletionResponse, ttl: Optional[int] = None
    ) -> None:
        """Store a response in the cache."""
        try:
            ttl = ttl if ttl is not None else self.default_ttl
            raw = response.model_dump_json()
            payload = _maybe_compress(raw)
            await self.redis.set(key, payload, ex=ttl)
            logger.debug(
                "Cache SET for key %s TTL=%ss size=%d (compressed=%s)",
                key[-8:], ttl, len(payload), payload.startswith(_GZIP_MARKER),
            )
        except Exception as e:
            logger.warning(f"Cache set error for key {key[-8:]}: {e}")

    async def get_stats(self) -> dict:
        """Return basic cache statistics from Redis."""
        try:
            pattern = f"{CACHE_KEY_PREFIX}*"
            keys = []
            async for k in self.redis.scan_iter(pattern, count=500):
                keys.append(k)
            return {"cached_responses": len(keys)}
        except Exception as e:
            logger.warning(f"Cache stats error: {e}")
            return {"cached_responses": 0}
