import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Analytics"])

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "static",
)

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "CDN-Cache-Control": "no-store",
}

# Free-tier model identifiers (partial match)
_FREE_TIER_MODELS = {
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-2.5-flash",
    "gemini-flash",
    "mistral-small",
    "llama",
    "llama3",
}


def _is_free_tier(model_id: str) -> bool:
    lower = model_id.lower()
    return any(kw in lower for kw in _FREE_TIER_MODELS)


def _provider_from_model(model_id: str) -> str:
    """Infer provider name from model ID heuristic."""
    lower = model_id.lower()
    if "gemini" in lower:
        return "gemini"
    if "gpt" in lower or "o1" in lower or "o3" in lower:
        return "openai"
    if "claude" in lower:
        return "anthropic"
    if "mistral" in lower or "mixtral" in lower:
        return "mistral"
    if "llama" in lower or "meta" in lower:
        return "meta"
    if "deepseek" in lower:
        return "deepseek"
    if "groq" in lower:
        return "groq"
    return "unknown"


@router.get("/analytics", response_class=HTMLResponse, summary="Analytics dashboard")
async def analytics_page() -> HTMLResponse:
    """Serve the analytics HTML dashboard."""
    path = os.path.join(_STATIC_DIR, "analytics.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200, headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Analytics page not found</h1>", status_code=404)


@router.get("/analytics/data", summary="Analytics data JSON")
async def analytics_data(request: Request) -> JSONResponse:
    """Return aggregated analytics data from Redis."""
    redis = request.app.state.redis

    async def get_int(key: str) -> int:
        try:
            val = await redis.get(key)
            return int(val) if val else 0
        except Exception:
            return 0

    # ── Summary counters ──────────────────────────────────────────────────────
    requests_total = await get_int("arbiter:stats:requests_total")
    requests_success = await get_int("arbiter:stats:requests_success")
    requests_failed = await get_int("arbiter:stats:requests_failed")
    cache_hits = await get_int("arbiter:stats:cache_hits")
    cache_misses = await get_int("arbiter:stats:cache_misses")

    total_cache_lookups = cache_hits + cache_misses
    cache_hit_rate = (
        round(cache_hits / total_cache_lookups * 100, 1)
        if total_cache_lookups > 0
        else 0.0
    )
    success_rate = (
        round(requests_success / requests_total * 100, 1)
        if requests_total > 0
        else 0.0
    )

    # ── Per-provider stats ────────────────────────────────────────────────────
    provider_stats = []
    try:
        provider_keys = await redis.keys("arbiter:stats:provider:*:success")
    except Exception:
        provider_keys = []

    seen_providers: set[str] = set()
    for key in provider_keys:
        # key format: arbiter:stats:provider:{name}:success
        parts = key.split(":")
        if len(parts) < 5:
            continue
        provider_name = parts[3]
        if provider_name in seen_providers:
            continue
        seen_providers.add(provider_name)

        p_success = await get_int(f"arbiter:stats:provider:{provider_name}:success")
        p_errors = await get_int(f"arbiter:stats:provider:{provider_name}:errors")
        p_rate_limited = await get_int(f"arbiter:stats:provider:{provider_name}:rate_limited")
        p_total = p_success + p_errors + p_rate_limited

        lat_sum = await get_int(f"arbiter:stats:latency:{provider_name}:sum")
        lat_count = await get_int(f"arbiter:stats:latency:{provider_name}:count")
        avg_latency = round(lat_sum / lat_count) if lat_count > 0 else 0

        p_success_rate = round(p_success / p_total * 100, 1) if p_total > 0 else 0.0

        provider_stats.append(
            {
                "name": provider_name,
                "requests": p_total,
                "success": p_success,
                "errors": p_errors,
                "rate_limited": p_rate_limited,
                "success_rate": p_success_rate,
                "avg_latency_ms": avg_latency,
            }
        )

    provider_stats.sort(key=lambda p: p["requests"], reverse=True)

    # ── Global avg latency ────────────────────────────────────────────────────
    total_lat_sum = sum(p["avg_latency_ms"] * await get_int(
        f"arbiter:stats:latency:{p['name']}:count"
    ) for p in provider_stats)
    total_lat_count = 0
    for p in provider_stats:
        try:
            cnt = await get_int(f"arbiter:stats:latency:{p['name']}:count")
            total_lat_count += cnt
        except Exception:
            pass
    # Re-compute correctly: sum(lat_sum per provider)
    agg_lat_sum = 0
    agg_lat_count = 0
    for p_name in seen_providers:
        agg_lat_sum += await get_int(f"arbiter:stats:latency:{p_name}:sum")
        agg_lat_count += await get_int(f"arbiter:stats:latency:{p_name}:count")
    avg_latency_ms = round(agg_lat_sum / agg_lat_count) if agg_lat_count > 0 else 0

    # ── Per-model stats ───────────────────────────────────────────────────────
    model_stats = []
    try:
        model_keys = await redis.keys("arbiter:stats:model:*:requests")
    except Exception:
        model_keys = []

    for key in model_keys:
        # key format: arbiter:stats:model:{model_id}:requests
        # model_id itself may contain colons so we split carefully
        prefix = "arbiter:stats:model:"
        suffix = ":requests"
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        model_id = key[len(prefix): -len(suffix)]
        if not model_id:
            continue

        m_requests = await get_int(f"arbiter:stats:model:{model_id}:requests")
        m_tokens = await get_int(f"arbiter:stats:model:{model_id}:tokens")
        m_errors = await get_int(f"arbiter:stats:model:{model_id}:errors")
        m_success = m_requests - m_errors
        m_success_rate = round(m_success / m_requests * 100, 1) if m_requests > 0 else 0.0

        model_stats.append(
            {
                "model_id": model_id,
                "provider": _provider_from_model(model_id),
                "requests": m_requests,
                "tokens": m_tokens,
                "errors": m_errors,
                "success_rate": m_success_rate,
                "free_tier": _is_free_tier(model_id),
            }
        )

    model_stats.sort(key=lambda m: m["requests"], reverse=True)

    # ── History buckets ───────────────────────────────────────────────────────
    history: list[dict] = []
    try:
        hist_keys = await redis.keys("arbiter:stats:history:*")
    except Exception:
        hist_keys = []

    # Collect unique timestamps
    ts_map: dict[str, dict] = {}
    for key in hist_keys:
        # key format: arbiter:stats:history:{timestamp_5min}:{metric}
        parts = key.split(":")
        if len(parts) < 5:
            continue
        ts = parts[3]
        metric = parts[4]
        if ts not in ts_map:
            ts_map[ts] = {"ts": int(ts) if ts.isdigit() else 0, "requests": 0, "success": 0, "errors": 0}
        try:
            val = await get_int(key)
        except Exception:
            val = 0
        if metric == "requests":
            ts_map[ts]["requests"] = val
        elif metric == "success":
            ts_map[ts]["success"] = val
        elif metric in ("errors", "failed"):
            ts_map[ts]["errors"] = val

    history = sorted(ts_map.values(), key=lambda x: x["ts"])
    # Return last 20 buckets
    history = history[-20:]

    return JSONResponse(
        {
            "summary": {
                "total_requests": requests_total,
                "success": requests_success,
                "failed": requests_failed,
                "success_rate": success_rate,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "cache_hit_rate": cache_hit_rate,
                "avg_latency_ms": avg_latency_ms,
            },
            "providers": provider_stats,
            "models": model_stats,
            "history": history,
        }
    )


@router.delete("/analytics/reset", summary="Reset all analytics counters")
async def analytics_reset(request: Request) -> JSONResponse:
    """Delete all arbiter:stats:* keys from Redis, resetting analytics."""
    redis = request.app.state.redis
    deleted = 0
    try:
        keys = await redis.keys("arbiter:stats:*")
        if keys:
            deleted = await redis.delete(*keys)
        logger.info("Analytics reset: deleted %d Redis keys", deleted)
    except Exception as exc:
        logger.error("Analytics reset failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "deleted_keys": deleted})
