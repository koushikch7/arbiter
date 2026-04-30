import logging
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.observability import stats as obs_stats

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
async def analytics_data(
    request: Request,
    from_date: Optional[str] = Query(None, alias="from",
        description="ISO date YYYY-MM-DD (inclusive). When set with `to`, returns daily-rollup data."),
    to_date: Optional[str] = Query(None, alias="to",
        description="ISO date YYYY-MM-DD (inclusive)."),
    token_id: Optional[str] = Query(None,
        description="Filter to a single gateway token id (or 'env' for env-var traffic)."),
    provider_filter: Optional[str] = Query(None, alias="provider",
        description="Filter to a single provider name."),
    model_filter: Optional[str] = Query(None, alias="model",
        description="Filter to a single model id."),
) -> JSONResponse:
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

    # ── Per-gateway-token usage (lifetime) ────────────────────────────────────
    tokens_data: list[dict] = []
    try:
        # Discover all tokens that have any traffic
        tok_keys = await redis.keys("arbiter:stats:token:*:requests")
        seen_tokens: set[str] = set()
        for k in tok_keys:
            parts = k.split(":")
            # arbiter:stats:token:{id}:requests
            if len(parts) == 5 and parts[2] == "token":
                seen_tokens.add(parts[3])
        # Also enumerate registered tokens (may have zero traffic)
        try:
            raw = await redis.get("arbiter:gateway:tokens")
            if raw:
                for t in json.loads(raw):
                    seen_tokens.add(t.get("id", ""))
        except Exception:
            pass
        seen_tokens.discard("")

        # Resolve names from the registry
        token_names: dict[str, str] = {"env": "env-var"}
        try:
            raw = await redis.get("arbiter:gateway:tokens")
            if raw:
                for t in json.loads(raw):
                    token_names[t.get("id", "")] = t.get("name", "")
        except Exception:
            pass

        for tid in sorted(seen_tokens):
            summary = await obs_stats.get_token_summary(redis, tid)
            tokens_data.append({
                "id":           tid,
                "name":         token_names.get(tid, ""),
                "requests":     summary["requests"],
                "success":      summary["success"],
                "errors":       summary["errors"],
                "tokens":       summary["tokens"],
                "last_used_at": summary["last_used_at"],
            })
        tokens_data.sort(key=lambda t: -t["requests"])
    except Exception as e:
        logger.warning("Token stats error: %s", e)

    # ── Date-range filtered view (optional) ───────────────────────────────────
    range_data: Optional[dict] = None
    if from_date and to_date:
        days = obs_stats.daterange(from_date, to_date)
        series: list[dict] = []
        total_req = total_ok = total_err = total_tok = 0
        per_provider_rng: dict[str, int] = {}
        per_model_rng: dict[str, int] = {}
        per_token_rng: dict[str, int] = {}

        for d in days:
            if token_id:
                tid = obs_stats._safe(token_id, 40)
                req = await get_int(f"arbiter:stats:day:{d}:token:{tid}:requests")
                tok = await get_int(f"arbiter:stats:day:{d}:token:{tid}:tokens")
                ok  = req  # token-level counters track all (success path)
                err = 0
            elif provider_filter:
                p = obs_stats._safe(provider_filter, 40)
                req = await get_int(f"arbiter:stats:day:{d}:provider:{p}:requests")
                tok = 0
                ok  = req
                err = 0
            elif model_filter:
                m = obs_stats._safe(model_filter, 80)
                req = await get_int(f"arbiter:stats:day:{d}:model:{m}:requests")
                tok = 0
                ok  = req
                err = 0
            else:
                req = await get_int(f"arbiter:stats:day:{d}:requests")
                ok  = await get_int(f"arbiter:stats:day:{d}:success")
                err = await get_int(f"arbiter:stats:day:{d}:errors")
                tok = await get_int(f"arbiter:stats:day:{d}:tokens")
            series.append({"date": d, "requests": req, "success": ok,
                           "errors": err, "tokens": tok})
            total_req += req; total_ok += ok; total_err += err; total_tok += tok

        # Per-provider/model/token aggregates over the range
        if not (provider_filter or model_filter or token_id):
            for d in days:
                # Providers
                try:
                    pkeys = await redis.keys(f"arbiter:stats:day:{d}:provider:*:requests")
                    for pk in pkeys:
                        pn = pk.split(":")[5]
                        per_provider_rng[pn] = per_provider_rng.get(pn, 0) + \
                            await get_int(pk)
                except Exception:
                    pass
                # Models
                try:
                    mkeys = await redis.keys(f"arbiter:stats:day:{d}:model:*:requests")
                    for mk in mkeys:
                        mn = mk.split(":")[5]
                        per_model_rng[mn] = per_model_rng.get(mn, 0) + \
                            await get_int(mk)
                except Exception:
                    pass
                # Tokens
                try:
                    tkeys = await redis.keys(f"arbiter:stats:day:{d}:token:*:requests")
                    for tk in tkeys:
                        tn = tk.split(":")[5]
                        per_token_rng[tn] = per_token_rng.get(tn, 0) + \
                            await get_int(tk)
                except Exception:
                    pass

        range_data = {
            "from":         days[0] if days else None,
            "to":           days[-1] if days else None,
            "totals": {
                "requests": total_req, "success": total_ok,
                "errors":   total_err, "tokens":  total_tok,
                "success_rate": round(total_ok / total_req * 100, 1) if total_req else 0.0,
            },
            "series":       series,
            "by_provider":  sorted(
                [{"provider": k, "requests": v} for k, v in per_provider_rng.items()],
                key=lambda x: -x["requests"]),
            "by_model":     sorted(
                [{"model": k, "requests": v} for k, v in per_model_rng.items()],
                key=lambda x: -x["requests"])[:25],
            "by_token":     sorted(
                [{"token_id": k, "requests": v} for k, v in per_token_rng.items()],
                key=lambda x: -x["requests"]),
            "filters": {
                "token_id": token_id, "provider": provider_filter,
                "model":    model_filter,
            },
        }

    # ── Auth-mode flag (UI shows banner when auth is open) ────────────────────
    try:
        from app.config import settings
        env_keys = settings.get_gateway_api_keys()
        dyn = bool(getattr(request.app.state, "gateway_tokens", set()))
        auth_enforced = bool(env_keys) or dyn
    except Exception:
        auth_enforced = False

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
            "auth_enforced":   auth_enforced,
        },
        "providers":         provider_stats,
        "models":            model_stats,
        "history":           history,
        "key_pools":         key_pools_data,
        "token_by_provider": token_by_provider,
        "tokens":            tokens_data,
        "range":             range_data,
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
