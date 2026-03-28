"""Settings API — runtime routing config management."""
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.routing.router import VENDOR_MODEL_HIERARCHY, _DEFAULT_PROVIDER_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["Settings"])

_KEY_ORDER  = "arbiter:config:provider_order"
_KEY_MODELS = "arbiter:config:models:"


@router.get("/routing")
async def get_routing(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    raw = await redis.get(_KEY_ORDER)
    provider_order = json.loads(raw) if raw else list(_DEFAULT_PROVIDER_ORDER)
    overrides = {}
    for p in _DEFAULT_PROVIDER_ORDER:
        r = await redis.get(f"{_KEY_MODELS}{p}")
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


@router.post("/routing")
async def save_routing(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    body = await request.json()
    if "provider_order" in body:
        await redis.set(_KEY_ORDER, json.dumps(body["provider_order"]))
    for p, models in body.get("model_overrides", {}).items():
        await redis.set(f"{_KEY_MODELS}{p}", json.dumps(models))
    return JSONResponse({"status": "saved"})


@router.delete("/routing")
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


@router.delete("/cache")
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
