"""
Centralized stats / analytics counters.

All metrics are written through this module so that the schema is consistent
and a single grep finds every key prefix.

Key namespaces (all keys are prefixed with ``arbiter:``):

Global counters (lifetime since last reset)::

    stats:requests_total              int
    stats:requests_success            int
    stats:requests_failed             int
    stats:cache_hits                  int
    stats:cache_misses                int

Per-provider (lifetime)::

    stats:provider:{name}:success     int
    stats:provider:{name}:errors      int
    stats:provider:{name}:rate_limited int
    stats:latency:{name}:sum          int  (ms)
    stats:latency:{name}:count        int

Per-model (lifetime)::

    stats:model:{model}:requests      int
    stats:model:{model}:tokens        int
    stats:model:{model}:errors        int
    stats:latency:model:{model}:sum   int  (ms)
    stats:latency:model:{model}:count int

Per-token (lifetime)::

    stats:token:{id}:requests         int
    stats:token:{id}:success          int
    stats:token:{id}:errors           int
    stats:token:{id}:tokens           int
    stats:token:{id}:last_used        float (epoch seconds)
    stats:token:{id}:provider:{name}:requests   int
    stats:token:{id}:model:{model}:requests     int

Time-series (5-min buckets, last 4 hours preserved by analytics)::

    stats:history:{bucket_ts}:requests
    stats:history:{bucket_ts}:success
    stats:history:{bucket_ts}:errors

Daily rollups (90-day TTL, used for date-range filtering)::

    stats:day:{YYYY-MM-DD}:requests              int
    stats:day:{YYYY-MM-DD}:success               int
    stats:day:{YYYY-MM-DD}:errors                int
    stats:day:{YYYY-MM-DD}:tokens                int
    stats:day:{YYYY-MM-DD}:provider:{name}:requests int
    stats:day:{YYYY-MM-DD}:model:{model}:requests   int
    stats:day:{YYYY-MM-DD}:token:{id}:requests      int
    stats:day:{YYYY-MM-DD}:token:{id}:tokens        int

Functions are no-ops when ``redis`` is None — safe in tests/dev.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 90 days of daily rollups should be plenty for analytics filtering.
_DAILY_TTL_SECONDS  = 90 * 24 * 3600
# 5-min history buckets: keep 7 days (TTL prevents volatile-lru eviction).
_HISTORY_5M_TTL     =  7 * 24 * 3600
# Hourly rollup buckets: keep 30 days (powers 24h / 7d analytics windows).
_HISTORY_1H_TTL     = 30 * 24 * 3600

_INVALID_KEY_CHARS = re.compile(r"[^a-zA-Z0-9._\-]")


def _safe(name: str, max_len: int = 80) -> str:
    """Sanitise a free-form id for use in a Redis key."""
    if not name:
        return "unknown"
    name = _INVALID_KEY_CHARS.sub("_", name)
    return name[:max_len]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _incr(redis, key: str, amount: int = 1, ttl: Optional[int] = None) -> None:
    if redis is None or amount <= 0:
        return
    try:
        if amount == 1:
            await redis.incr(key)
        else:
            await redis.incrby(key, amount)
        if ttl is not None:
            try:
                await redis.expire(key, ttl)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("stats incr failed (%s): %s", key, exc)


async def _set(redis, key: str, value: str, ttl: Optional[int] = None) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=ttl)
    except Exception as exc:
        logger.debug("stats set failed (%s): %s", key, exc)


# ---------------------------------------------------------------------------
# Public API — call these from the router / middleware
# ---------------------------------------------------------------------------


async def record_success(
    redis,
    *,
    provider: str,
    model: str,
    tokens_used: int = 0,
    latency_ms: int = 0,
    token_id: Optional[str] = None,
) -> None:
    """Record a successful provider call (after cache miss)."""
    if redis is None:
        return
    day = _today()
    p = _safe(provider, 40)
    m = _safe(model, 80)

    # ── Lifetime counters ──────────────────────────────────────────────
    await _incr(redis, "arbiter:stats:requests_total")
    await _incr(redis, "arbiter:stats:requests_success")
    await _incr(redis, f"arbiter:stats:provider:{p}:success")
    await _incr(redis, f"arbiter:stats:model:{m}:requests")
    if tokens_used > 0:
        await _incr(redis, f"arbiter:stats:model:{m}:tokens", tokens_used)
    if latency_ms > 0:
        await _incr(redis, f"arbiter:stats:latency:{p}:sum", latency_ms)
        await _incr(redis, f"arbiter:stats:latency:{p}:count")
        await _incr(redis, f"arbiter:stats:latency:model:{m}:sum", latency_ms)
        await _incr(redis, f"arbiter:stats:latency:model:{m}:count")

    # ── 5-min history bucket (7-day TTL so volatile-lru never evicts) ──────
    bucket = (int(time.time()) // 300) * 300
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests", ttl=_HISTORY_5M_TTL)
    await _incr(redis, f"arbiter:stats:history:{bucket}:success",  ttl=_HISTORY_5M_TTL)

    # ── Hourly rollup bucket (30-day TTL, powers 24h / 7d analytics views) ──
    hour = (int(time.time()) // 3600) * 3600
    await _incr(redis, f"arbiter:stats:hourly:{hour}:requests", ttl=_HISTORY_1H_TTL)
    await _incr(redis, f"arbiter:stats:hourly:{hour}:success",  ttl=_HISTORY_1H_TTL)
    if tokens_used > 0:
        await _incr(redis, f"arbiter:stats:hourly:{hour}:tokens", tokens_used, ttl=_HISTORY_1H_TTL)

    # ── Daily rollup (90-day TTL) ──────────────────────────────────────
    ttl = _DAILY_TTL_SECONDS
    await _incr(redis, f"arbiter:stats:day:{day}:requests", ttl=ttl)
    await _incr(redis, f"arbiter:stats:day:{day}:success", ttl=ttl)
    await _incr(redis, f"arbiter:stats:day:{day}:provider:{p}:requests", ttl=ttl)
    await _incr(redis, f"arbiter:stats:day:{day}:model:{m}:requests", ttl=ttl)
    if tokens_used > 0:
        await _incr(redis, f"arbiter:stats:day:{day}:tokens", tokens_used, ttl=ttl)

    # ── Per-token counters ─────────────────────────────────────────────
    if token_id:
        tid = _safe(token_id, 40)
        await _incr(redis, f"arbiter:stats:token:{tid}:requests")
        await _incr(redis, f"arbiter:stats:token:{tid}:success")
        if tokens_used > 0:
            await _incr(redis, f"arbiter:stats:token:{tid}:tokens", tokens_used)
        await _incr(redis, f"arbiter:stats:token:{tid}:provider:{p}:requests")
        await _incr(redis, f"arbiter:stats:token:{tid}:model:{m}:requests")
        await _set(redis, f"arbiter:stats:token:{tid}:last_used", str(time.time()))
        # Daily per-token
        await _incr(redis, f"arbiter:stats:day:{day}:token:{tid}:requests", ttl=ttl)
        if tokens_used > 0:
            await _incr(redis, f"arbiter:stats:day:{day}:token:{tid}:tokens",
                        tokens_used, ttl=ttl)


async def record_failure(
    redis,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    rate_limited: bool = False,
    token_id: Optional[str] = None,
) -> None:
    """Record a failed provider call (after exhausting all retries / cooldowns)."""
    if redis is None:
        return
    day = _today()
    ttl = _DAILY_TTL_SECONDS

    # Caller-side classification (a single attempt that errored)
    if provider:
        p = _safe(provider, 40)
        if rate_limited:
            await _incr(redis, f"arbiter:stats:provider:{p}:rate_limited")
        else:
            await _incr(redis, f"arbiter:stats:provider:{p}:errors")

    if model:
        m = _safe(model, 80)
        await _incr(redis, f"arbiter:stats:model:{m}:errors")


async def record_request_failed(
    redis,
    *,
    token_id: Optional[str] = None,
) -> None:
    """Record a top-level request failure (all candidates exhausted)."""
    if redis is None:
        return
    day = _today()
    ttl = _DAILY_TTL_SECONDS

    await _incr(redis, "arbiter:stats:requests_total")
    await _incr(redis, "arbiter:stats:requests_failed")
    await _incr(redis, f"arbiter:stats:day:{day}:requests", ttl=ttl)
    await _incr(redis, f"arbiter:stats:day:{day}:errors", ttl=ttl)

    bucket = (int(time.time()) // 300) * 300
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests", ttl=_HISTORY_5M_TTL)
    await _incr(redis, f"arbiter:stats:history:{bucket}:errors",   ttl=_HISTORY_5M_TTL)
    hour = (int(time.time()) // 3600) * 3600
    await _incr(redis, f"arbiter:stats:hourly:{hour}:requests", ttl=_HISTORY_1H_TTL)
    await _incr(redis, f"arbiter:stats:hourly:{hour}:errors",   ttl=_HISTORY_1H_TTL)

    if token_id:
        tid = _safe(token_id, 40)
        await _incr(redis, f"arbiter:stats:token:{tid}:requests")
        await _incr(redis, f"arbiter:stats:token:{tid}:errors")
        await _set(redis, f"arbiter:stats:token:{tid}:last_used", str(time.time()))
        await _incr(redis, f"arbiter:stats:day:{day}:token:{tid}:requests", ttl=ttl)


async def record_cache_hit(redis, token_id: Optional[str] = None) -> None:
    if redis is None:
        return
    day = _today()
    ttl = _DAILY_TTL_SECONDS
    await _incr(redis, "arbiter:stats:cache_hits")
    await _incr(redis, "arbiter:stats:requests_total")
    await _incr(redis, "arbiter:stats:requests_success")
    await _incr(redis, f"arbiter:stats:day:{day}:requests", ttl=ttl)
    await _incr(redis, f"arbiter:stats:day:{day}:success", ttl=ttl)
    bucket = (int(time.time()) // 300) * 300
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests", ttl=_HISTORY_5M_TTL)
    await _incr(redis, f"arbiter:stats:history:{bucket}:success",  ttl=_HISTORY_5M_TTL)
    hour = (int(time.time()) // 3600) * 3600
    await _incr(redis, f"arbiter:stats:hourly:{hour}:requests", ttl=_HISTORY_1H_TTL)
    await _incr(redis, f"arbiter:stats:hourly:{hour}:success",  ttl=_HISTORY_1H_TTL)
    if token_id:
        tid = _safe(token_id, 40)
        await _incr(redis, f"arbiter:stats:token:{tid}:requests")
        await _incr(redis, f"arbiter:stats:token:{tid}:success")
        await _set(redis, f"arbiter:stats:token:{tid}:last_used", str(time.time()))
        await _incr(redis, f"arbiter:stats:day:{day}:token:{tid}:requests", ttl=ttl)


async def record_cache_miss(redis) -> None:
    if redis is None:
        return
    await _incr(redis, "arbiter:stats:cache_misses")


# ---------------------------------------------------------------------------
# Structured error log — stores recent failures with context for daily report
# ---------------------------------------------------------------------------

# Sorted-set key (v1.18.0+). The legacy list key is kept as a fallback
# during the migration window so already-recorded errors are still visible
# in the daily report until they roll out of the 48h retention window.
_ERROR_LOG_KEY        = "arbiter:error_log_z"
_ERROR_LOG_KEY_LEGACY = "arbiter:error_log"
_ERROR_LOG_MAX = 50
# Retention window for the sorted-set variant; sliding 48h.
_ERROR_LOG_TTL_SECONDS = 48 * 3600


async def record_error_detail(
    redis,
    *,
    provider: str,
    model: str,
    error_type: str,
    error_message: str,
    rate_limited: bool = False,
) -> None:
    """Store a structured error record in a Redis sorted set for the daily report.

    Uses ``ZADD`` with the event timestamp as score, plus ``ZREMRANGEBYSCORE``
    to evict entries older than 48 hours. This is O(log N) per write versus
    the previous O(N) ``LTRIM`` on the legacy list implementation, and the
    score-based eviction means entries naturally age out instead of being
    bounded purely by count.
    """
    if redis is None:
        return
    import json
    now = time.time()
    entry = json.dumps({
        "ts": now,
        "provider": provider,
        "model": model,
        "type": error_type,
        "msg": error_message[:300],  # truncate long messages
        "rate_limited": rate_limited,
    })
    try:
        await redis.zadd(_ERROR_LOG_KEY, {entry: now})
        # Evict entries older than the retention window.
        await redis.zremrangebyscore(_ERROR_LOG_KEY, 0, now - _ERROR_LOG_TTL_SECONDS)
        # Safety cap: trim to the most recent _ERROR_LOG_MAX * 4 entries
        # (allows up to ~200 records in the 48h window without unbounded growth).
        await redis.zremrangebyrank(_ERROR_LOG_KEY, 0, -(_ERROR_LOG_MAX * 4) - 1)
        await redis.expire(_ERROR_LOG_KEY, _ERROR_LOG_TTL_SECONDS)
    except Exception as exc:
        logger.debug("record_error_detail failed: %s", exc)


async def get_recent_errors(redis, limit: int = 30) -> list:
    """Retrieve the most recent structured error records (newest first)."""
    if redis is None:
        return []
    import json
    try:
        # ZREVRANGE returns highest-score (most recent) first.
        raw_list = await redis.zrevrange(_ERROR_LOG_KEY, 0, max(0, limit - 1))
        out = [json.loads(e) for e in raw_list if e]
    except Exception:
        out = []
    # Backfill from the legacy list key while it still has unexpired entries.
    if len(out) < limit:
        try:
            legacy = await redis.lrange(_ERROR_LOG_KEY_LEGACY, 0, limit - len(out) - 1)
            out.extend(json.loads(e) for e in legacy if e)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Read helpers (used by analytics_api & gateway_tokens_api)
# ---------------------------------------------------------------------------


async def get_int(redis, key: str) -> int:
    if redis is None:
        return 0
    try:
        v = await redis.get(key)
        return int(v) if v else 0
    except Exception:
        return 0


async def get_float(redis, key: str) -> float:
    if redis is None:
        return 0.0
    try:
        v = await redis.get(key)
        return float(v) if v else 0.0
    except Exception:
        return 0.0


async def get_token_summary(redis, token_id: str) -> dict:
    """Return live counters for a single gateway token."""
    tid = _safe(token_id, 40)
    if redis is None:
        return {
            "requests": 0, "success": 0, "errors": 0,
            "tokens": 0, "last_used_at": None,
        }
    requests = await get_int(redis, f"arbiter:stats:token:{tid}:requests")
    success  = await get_int(redis, f"arbiter:stats:token:{tid}:success")
    errors   = await get_int(redis, f"arbiter:stats:token:{tid}:errors")
    tokens   = await get_int(redis, f"arbiter:stats:token:{tid}:tokens")
    last     = await get_float(redis, f"arbiter:stats:token:{tid}:last_used")
    return {
        "requests":     requests,
        "success":      success,
        "errors":       errors,
        "tokens":       tokens,
        "last_used_at": last if last > 0 else None,
    }


def daterange(from_date: str, to_date: str) -> list[str]:
    """Return inclusive list of YYYY-MM-DD between from_date and to_date."""
    from datetime import datetime as _dt, timedelta
    try:
        d0 = _dt.strptime(from_date, "%Y-%m-%d").date()
        d1 = _dt.strptime(to_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


async def get_model_performance(redis) -> dict[str, dict]:
    """Return per-model error-rate + avg latency from lifetime counters.

    Used by the router to sort candidates by observed performance.
    Returns a mapping of model_id → {error_rate, avg_latency_ms, requests}.
    Only models with ≥5 requests are included so cold-start models retain
    neutral score.
    """
    if redis is None:
        return {}
    perf: dict[str, dict] = {}
    try:
        async for key in redis.scan_iter("arbiter:stats:model:*:requests"):
            prefix = "arbiter:stats:model:"
            suffix = ":requests"
            if not (key.startswith(prefix) and key.endswith(suffix)):
                continue
            model_id = key[len(prefix):-len(suffix)]
            if not model_id:
                continue
            try:
                req_v  = await redis.get(f"arbiter:stats:model:{model_id}:requests")
                err_v  = await redis.get(f"arbiter:stats:model:{model_id}:errors")
                ls_v   = await redis.get(f"arbiter:stats:latency:model:{model_id}:sum")
                lc_v   = await redis.get(f"arbiter:stats:latency:model:{model_id}:count")
                req    = int(req_v) if req_v else 0
                err    = int(err_v) if err_v else 0
                ls     = int(ls_v)  if ls_v  else 0
                lc     = int(lc_v)  if lc_v  else 0
                if req < 5:
                    continue
                perf[model_id] = {
                    "requests":      req,
                    "error_rate":    round(err / req, 4) if req > 0 else 0.0,
                    "avg_latency_ms": round(ls / lc) if lc > 0 else 0,
                }
            except Exception:
                continue
    except Exception:
        pass
    return perf
