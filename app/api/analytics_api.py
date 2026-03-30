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
    "Pragma":        "no-cache",
    "CDN-Cache-Control": "no-store",
}

_PROVIDER_HINTS = {
    "gemini": "gemini",
    "gpt": "openai", "o1": "openai", "o3": "openai",
    "claude": "anthropic",
    "mistral": "mistral", "mixtral": "mistral",
    "llama": "meta", "meta-llama": "meta",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "@cf/": "cloudflare",
    "cohere": "cohere",
    "cerebras": "cerebras",
}

_FREE_TIER_KEYWORDS = {
    "gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash",
    "gemini-flash", "mistral-small", "llama", "llama3", ":free",
}


def _is_free_tier(model_id: str) -> bool:
    lower = model_id.lower()
    return any(kw in lower for kw in _FREE_TIER_KEYWORDS)


def _provider_from_model(model_id: str) -> str:
    lower = model_id.lower()
    for kw, provider in _PROVIDER_HINTS.items():
        if kw in lower:
            return provider
    return "unknown"


def _health_label(total: int, active: int) -> str:
    if total == 0:
        return "unconfigured"
    ratio = active / total
    if ratio > 0.5:
        return "healthy"
    if ratio > 0:
        return "degraded"
    return "unavailable"


def _gauge_color(pct: float) -> str:
    if pct < 60:
        return "green"
    if pct < 85:
        return "yellow"
    return "red"


@router.get("/analytics", response_class=HTMLResponse, summary="Analytics dashboard")
async def analytics_page() -> HTMLResponse:
    path = os.path.join(_STATIC_DIR, "analytics.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200, headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Analytics page not found</h1>", status_code=404)


@router.get("/analytics/data", summary="Analytics data JSON")
async def analytics_data(request: Request) -> JSONResponse:
    redis = request.app.state.redis

    async def get_int(key: str) -> int:
        try:
            val = await redis.get(key)
            return int(val) if val else 0
        except Exception:
            return 0

    # ── Global counters ───────────────────────────────────────────────────────
    requests_total   = await get_int("arbiter:stats:requests_total")
    requests_success = await get_int("arbiter:stats:requests_success")
    requests_failed  = await get_int("arbiter:stats:requests_failed")
    cache_hits       = await get_int("arbiter:stats:cache_hits")
    cache_misses     = await get_int("arbiter:stats:cache_misses")

    total_cache_lookups = cache_hits + cache_misses
    cache_hit_rate = round(cache_hits / total_cache_lookups * 100, 1) if total_cache_lookups > 0 else 0.0
    success_rate   = round(requests_success / requests_total * 100, 1) if requests_total > 0 else 0.0

    # ── Per-provider stats ────────────────────────────────────────────────────
    provider_stats: list[dict] = []
    seen_providers: set[str] = set()

    try:
        provider_keys = await redis.keys("arbiter:stats:provider:*:success")
    except Exception:
        provider_keys = []

    for key in provider_keys:
        parts = key.split(":")
        if len(parts) < 5:
            continue
        pname = parts[3]
        if pname in seen_providers:
            continue
        seen_providers.add(pname)

        p_success      = await get_int(f"arbiter:stats:provider:{pname}:success")
        p_errors       = await get_int(f"arbiter:stats:provider:{pname}:errors")
        p_rate_limited = await get_int(f"arbiter:stats:provider:{pname}:rate_limited")
        p_total        = p_success + p_errors + p_rate_limited
        lat_sum        = await get_int(f"arbiter:stats:latency:{pname}:sum")
        lat_count      = await get_int(f"arbiter:stats:latency:{pname}:count")
        avg_lat        = round(lat_sum / lat_count) if lat_count > 0 else 0

        provider_stats.append({
            "name":          pname,
            "requests":      p_total,
            "success":       p_success,
            "errors":        p_errors,
            "rate_limited":  p_rate_limited,
            "success_rate":  round(p_success / p_total * 100, 1) if p_total > 0 else 0.0,
            "avg_latency_ms": avg_lat,
        })

    provider_stats.sort(key=lambda p: p["requests"], reverse=True)

    # ── Global avg latency ────────────────────────────────────────────────────
    agg_lat_sum = agg_lat_count = 0
    for pname in seen_providers:
        agg_lat_sum   += await get_int(f"arbiter:stats:latency:{pname}:sum")
        agg_lat_count += await get_int(f"arbiter:stats:latency:{pname}:count")
    avg_latency_ms = round(agg_lat_sum / agg_lat_count) if agg_lat_count > 0 else 0

    # ── Per-model stats ───────────────────────────────────────────────────────
    model_stats: list[dict] = []
    try:
        model_keys = await redis.keys("arbiter:stats:model:*:requests")
    except Exception:
        model_keys = []

    total_tokens = 0
    for key in model_keys:
        prefix = "arbiter:stats:model:"
        suffix = ":requests"
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        model_id = key[len(prefix):-len(suffix)]
        if not model_id:
            continue

        m_req    = await get_int(f"arbiter:stats:model:{model_id}:requests")
        m_tokens = await get_int(f"arbiter:stats:model:{model_id}:tokens")
        m_errors = await get_int(f"arbiter:stats:model:{model_id}:errors")
        total_tokens += m_tokens

        model_stats.append({
            "model_id":    model_id,
            "provider":    _provider_from_model(model_id),
            "requests":    m_req,
            "tokens":      m_tokens,
            "errors":      m_errors,
            "success_rate": round((m_req - m_errors) / m_req * 100, 1) if m_req > 0 else 0.0,
            "free_tier":   _is_free_tier(model_id),
        })

    model_stats.sort(key=lambda m: m["requests"], reverse=True)

    # ── Token totals per provider ─────────────────────────────────────────────
    token_by_provider: dict[str, int] = {}
    for m in model_stats:
        prov = m["provider"]
        token_by_provider[prov] = token_by_provider.get(prov, 0) + m["tokens"]

    # ── Time-series history (5-min buckets, last 48 = 4 hours) ───────────────
    ts_map: dict[str, dict] = {}
    try:
        hist_keys = await redis.keys("arbiter:stats:history:*")
    except Exception:
        hist_keys = []

    for key in hist_keys:
        parts = key.split(":")
        if len(parts) < 5:
            continue
        ts     = parts[3]
        metric = parts[4]
        if ts not in ts_map:
            ts_map[ts] = {"ts": int(ts) if ts.isdigit() else 0, "requests": 0, "success": 0, "errors": 0}
        val = await get_int(key)
        if metric == "requests":
            ts_map[ts]["requests"] = val
        elif metric == "success":
            ts_map[ts]["success"] = val
        elif metric in ("errors", "failed"):
            ts_map[ts]["errors"] = val

    history = sorted(ts_map.values(), key=lambda x: x["ts"])[-48:]

    # ── Key pool health (live) ────────────────────────────────────────────────
    key_pools_data: list[dict] = []
    active_keys_total     = 0
    configured_keys_total = 0

    try:
        kp = request.app.state.key_pools
        for pname, pool in kp.items():
            ps    = await pool.get_stats()
            total  = ps.get("total_keys", 0)
            active = ps.get("active_keys", 0)
            active_keys_total     += active
            configured_keys_total += total

            enriched = []
            for k in ps.get("keys", []):
                rl, tl, dl = k["rpm"]["limit"], k["tpm"]["limit"], k["daily"]["limit"]
                rp = round(k["rpm"]["used"]   / rl * 100, 1) if rl > 0 else 0
                tp = round(k["tpm"]["used"]   / tl * 100, 1) if tl > 0 else 0
                dp = round(k["daily"]["used"] / dl * 100, 1) if dl > 0 else 0
                enriched.append({
                    "hash":   k["hash"],
                    "status": k["status"],
                    "score":  round(k.get("score", 0), 3),
                    "rpm":   {"used": k["rpm"]["used"],   "limit": rl, "pct": rp,  "color": _gauge_color(rp)},
                    "tpm":   {"used": k["tpm"]["used"],   "limit": tl, "pct": tp,  "color": _gauge_color(tp)},
                    "daily": {"used": k["daily"]["used"], "limit": dl, "pct": dp,  "color": _gauge_color(dp)},
                })

            key_pools_data.append({
                "provider":    pname,
                "total_keys":  total,
                "active_keys": active,
                "status":      _health_label(total, active),
                "keys":        enriched,
            })
    except Exception as e:
        logger.warning("Key pool stats error: %s", e)

    key_pools_data.sort(key=lambda p: (-p["active_keys"], p["provider"]))

    return JSONResponse({
        "summary": {
            "total_requests":  requests_total,
            "success":         requests_success,
            "failed":          requests_failed,
            "success_rate":    success_rate,
            "cache_hits":      cache_hits,
            "cache_misses":    cache_misses,
            "cache_hit_rate":  cache_hit_rate,
            "avg_latency_ms":  avg_latency_ms,
            "total_tokens":    total_tokens,
            "active_keys":     active_keys_total,
            "configured_keys": configured_keys_total,
        },
        "providers":         provider_stats,
        "models":            model_stats,
        "history":           history,
        "key_pools":         key_pools_data,
        "token_by_provider": token_by_provider,
    })


@router.delete("/analytics/reset", summary="Reset all analytics counters")
async def analytics_reset(request: Request) -> JSONResponse:
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
