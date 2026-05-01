import hashlib
import json
import logging
from typing import Optional

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "arbiter:cache:"


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
        """
        messages_data = []
        for m in request.messages:
            messages_data.append({"role": m.role, "content": m.content})

        key_payload = json.dumps(
            {
                "model":      request.model,
                "messages":   messages_data,
                "max_tokens": request.max_tokens,
                "stop":       request.stop,
                "top_p":      request.top_p,
            },
            sort_keys=True,
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
            await self.redis.set(key, raw, ex=ttl)
            logger.debug(f"Cache SET for key {key[-8:]} TTL={ttl}s")
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
