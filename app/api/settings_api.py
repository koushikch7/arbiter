"""Settings API — runtime routing config management."""
import json
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from app.routing.router import VENDOR_MODEL_HIERARCHY, _DEFAULT_PROVIDER_ORDER
from app.api.users_api import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["Settings"])

_KEY_ORDER  = "arbiter:config:provider_order"
_KEY_MODELS = "arbiter:config:models:"


@router.get("/routing", dependencies=[Depends(require_admin)])
async def get_routing(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    raw = await redis.get(_KEY_ORDER)
    provider_order = json.loads(raw) if raw else list(_DEFAULT_PROVIDER_ORDER)
    # Fetch all model-override keys in one round-trip
    keys_to_fetch = [f"{_KEY_MODELS}{p}" for p in _DEFAULT_PROVIDER_ORDER]
    try:
        values = await redis.mget(*keys_to_fetch)
    except Exception:
        # _InMemoryRedis may not support mget — fall back to sequential gets
        values = [await redis.get(k) for k in keys_to_fetch]
    overrides = {}
    for p, r in zip(_DEFAULT_PROVIDER_ORDER, values):
        if r:
            overrides[p] = json.loads(r)
    return JSONResponse({
        "provider_order": provider_order,
        "default_provider_order": list(_DEFAULT_PROVIDER_ORDER),
        "model_hierarchies": {
            p: [{"model": m, "context_window": c} for m, c in models]
            for p, models in VENDOR_MODEL_HIERARCHY.items()
        },
        "model_overrides": overrides,
        "is_customized": bool(raw or overrides),
    })


@router.post("/routing", dependencies=[Depends(require_admin)])
async def save_routing(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    body = await request.json()
    if "provider_order" in body:
        await redis.set(_KEY_ORDER, json.dumps(body["provider_order"]))
    for p, models in body.get("model_overrides", {}).items():
        await redis.set(f"{_KEY_MODELS}{p}", json.dumps(models))
    return JSONResponse({"status": "saved"})


@router.delete("/routing", dependencies=[Depends(require_admin)])
async def reset_routing(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    try:
        await redis.delete(_KEY_ORDER)
    except Exception:
        pass
    for p in _DEFAULT_PROVIDER_ORDER:
        try:
            await redis.delete(f"{_KEY_MODELS}{p}")
        except Exception:
            pass
    return JSONResponse({"status": "reset", "provider_order": list(_DEFAULT_PROVIDER_ORDER)})


@router.delete("/cache", dependencies=[Depends(require_admin)])
async def clear_cache(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    count = 0
    async for key in redis.scan_iter("arbiter:cache:*"):
        try:
            await redis.delete(key)
            count += 1
        except Exception:
            pass
    return JSONResponse({"status": "cleared", "entries_deleted": count})


@router.get("/cache", dependencies=[Depends(require_admin)])
async def cache_info(request: Request) -> JSONResponse:
    """Return cache configuration + live counters for the Cache tab UI."""
    from app.config import settings
    from app.cache.cache import CACHE_KEY_PREFIX

    redis = request.app.state.redis

    async def _get_int(name: str) -> int:
        try:
            v = await redis.get(f"arbiter:stats:{name}")
            return int(v) if v else 0
        except Exception:
            return 0

    cache = request.app.state.cache
    stats = await cache.get_stats() if cache else {"cached_responses": 0}
    hits = await _get_int("cache_hits")
    misses = await _get_int("cache_misses")
    total = hits + misses
    hit_rate = round((hits / total) * 100, 1) if total else 0.0

    # Sample a few entries to show the user how big the cache is.
    sample = []
    try:
        async for k in redis.scan_iter(f"{CACHE_KEY_PREFIX}*", count=8):
            sample.append(k if isinstance(k, str) else k.decode())
            if len(sample) >= 8:
                break
    except Exception:
        pass

    return JSONResponse({
        "config": {
            "default_ttl_seconds": settings.CACHE_TTL,
            "deterministic_threshold": 0.3,
            "key_prefix": CACHE_KEY_PREFIX,
            "key_algorithm": "sha256(model + messages)",
            "redis_backend": True,
        },
        "stats": {
            "hits": hits,
            "misses": misses,
            "total_lookups": total,
            "hit_rate_pct": hit_rate,
            "cached_entries": stats.get("cached_responses", 0),
        },
        "sample_keys": [s[-16:] for s in sample],
    })
