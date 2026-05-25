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

    # ── Real-time: per-minute bucket + activity feed ──────────────────
    await incr_minute_bucket(redis, success=True, tokens=tokens_used)
    await record_recent_activity(
        redis,
        status="success",
        provider=provider,
        model=model,
        tokens=tokens_used,
        latency_ms=latency_ms,
        token_id=token_id,
    )


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

    # ── Real-time: per-minute bucket + activity feed ──────────────────
    await incr_minute_bucket(redis, success=False, is_error=True)
    await record_recent_activity(
        redis,
        status="error",
        provider=None,
        model=None,
        token_id=token_id,
        error="all candidates exhausted",
    )


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

    # ── Real-time: per-minute bucket + activity feed ──────────────────
    await incr_minute_bucket(redis, success=True)
    await record_recent_activity(
        redis,
        status="cache_hit",
        provider="cache",
        model=None,
        token_id=token_id,
    )


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


# ---------------------------------------------------------------------------
# Real-time gauges and recent-activity feed (v1.19.3+)
# ---------------------------------------------------------------------------

_INFLIGHT_KEY        = "arbiter:stats:inflight"
_RECENT_ACTIVITY_KEY = "arbiter:stats:recent_activity_z"
_RECENT_ACTIVITY_MAX = 100        # cap on stored entries
_RECENT_ACTIVITY_TTL = 4 * 3600   # 4-hour retention
# Per-minute history bucket (high-resolution view for 1h window)
_HISTORY_1M_TTL      = 3 * 3600   # keep 3 hours of per-minute data


async def inflight_increment(redis) -> None:
    """Atomic increment of in-flight requests gauge."""
    if redis is None:
        return
    try:
        await redis.incr(_INFLIGHT_KEY)
    except Exception as exc:
        logger.debug("inflight_increment failed: %s", exc)


async def inflight_decrement(redis) -> None:
    """Atomic decrement of in-flight requests gauge (clamped at 0)."""
    if redis is None:
        return
    try:
        val = await redis.decr(_INFLIGHT_KEY)
        if val is not None and int(val) < 0:
            # Clamp to 0 if a worker restart desynchronised the counter.
            await redis.set(_INFLIGHT_KEY, 0)
    except Exception as exc:
        logger.debug("inflight_decrement failed: %s", exc)


async def get_inflight(redis) -> int:
    if redis is None:
        return 0
    try:
        v = await redis.get(_INFLIGHT_KEY)
        return max(0, int(v)) if v else 0
    except Exception:
        return 0


async def record_recent_activity(
    redis,
    *,
    status: str,                 # "success" | "error" | "cache_hit"
    provider: Optional[str],
    model: Optional[str],
    tokens: int = 0,
    latency_ms: int = 0,
    token_id: Optional[str] = None,
    token_name: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Push a single request event into a sorted-set activity feed.

    Capped at _RECENT_ACTIVITY_MAX entries with sliding-window eviction
    on every write. Used by analytics dashboard's live activity feed.
    """
    if redis is None:
        return
    import json
    now = time.time()
    entry = json.dumps({
        "ts":       now,
        "status":   status,
        "provider": provider or "",
        "model":    model or "",
        "tokens":   int(tokens),
        "latency":  int(latency_ms),
        "token_id": token_id or "",
        "token_name": token_name or "",
        "error":    (error or "")[:200],
    })
    try:
        await redis.zadd(_RECENT_ACTIVITY_KEY, {entry: now})
        # Evict by age first, then enforce hard cap by rank.
        await redis.zremrangebyscore(_RECENT_ACTIVITY_KEY, 0, now - _RECENT_ACTIVITY_TTL)
        await redis.zremrangebyrank(_RECENT_ACTIVITY_KEY, 0, -(_RECENT_ACTIVITY_MAX) - 1)
        await redis.expire(_RECENT_ACTIVITY_KEY, _RECENT_ACTIVITY_TTL)
    except Exception as exc:
        logger.debug("record_recent_activity failed: %s", exc)


async def get_recent_activity(redis, limit: int = 30) -> list:
    """Retrieve the most recent request events (newest first)."""
    if redis is None:
        return []
    import json
    try:
        raw_list = await redis.zrevrange(_RECENT_ACTIVITY_KEY, 0, max(0, limit - 1))
        return [json.loads(e) for e in raw_list if e]
    except Exception:
        return []


async def incr_minute_bucket(
    redis,
    *,
    success: bool,
    is_error: bool = False,
    tokens: int = 0,
) -> None:
    """Increment the 1-minute time-series bucket (high-resolution)."""
    if redis is None:
        return
    minute = (int(time.time()) // 60) * 60
    base   = f"arbiter:stats:minute:{minute}"
    try:
        await _incr(redis, f"{base}:requests", ttl=_HISTORY_1M_TTL)
        if success:
            await _incr(redis, f"{base}:success", ttl=_HISTORY_1M_TTL)
        if is_error:
            await _incr(redis, f"{base}:errors", ttl=_HISTORY_1M_TTL)
        if tokens > 0:
            await _incr(redis, f"{base}:tokens", tokens, ttl=_HISTORY_1M_TTL)
    except Exception as exc:
        logger.debug("incr_minute_bucket failed: %s", exc)


async def get_rolling_rates(redis, *, window_seconds: int = 60) -> dict:
    """Compute requests/tokens rate over the last *window_seconds*.

    Reads per-minute buckets in the window and returns totals + per-second
    rates. Cheap (O(W/60) reads) and accurate to the minute boundary.
    """
    if redis is None:
        return {"requests": 0, "tokens": 0, "errors": 0, "rpm": 0.0, "tpm": 0.0}
    now      = int(time.time())
    n_buckets = max(1, window_seconds // 60)
    cur_min  = (now // 60) * 60
    req_sum = tok_sum = err_sum = 0
    for i in range(n_buckets):
        m = cur_min - i * 60
        try:
            r = await redis.get(f"arbiter:stats:minute:{m}:requests")
            t = await redis.get(f"arbiter:stats:minute:{m}:tokens")
            e = await redis.get(f"arbiter:stats:minute:{m}:errors")
            req_sum += int(r) if r else 0
            tok_sum += int(t) if t else 0
            err_sum += int(e) if e else 0
        except Exception:
            continue
    # Normalize to per-minute rate over the window.
    minutes = max(1.0, window_seconds / 60.0)
    return {
        "requests": req_sum,
        "tokens":   tok_sum,
        "errors":   err_sum,
        "rpm":      round(req_sum / minutes, 2),
        "tpm":      round(tok_sum / minutes, 2),
        "window_s": window_seconds,
    }


async def get_minute_history(redis, *, minutes: int = 60) -> list:
    """Return the last *minutes* of per-minute buckets, oldest-first."""
    if redis is None:
        return []
    now     = int(time.time())
    cur_min = (now // 60) * 60
    out: list = []
    for i in range(minutes - 1, -1, -1):
        m = cur_min - i * 60
        try:
            r = await redis.get(f"arbiter:stats:minute:{m}:requests")
            s = await redis.get(f"arbiter:stats:minute:{m}:success")
            e = await redis.get(f"arbiter:stats:minute:{m}:errors")
            t = await redis.get(f"arbiter:stats:minute:{m}:tokens")
        except Exception:
            r = s = e = t = None
        out.append({
            "ts":       m,
            "requests": int(r) if r else 0,
            "success":  int(s) if s else 0,
            "errors":   int(e) if e else 0,
            "tokens":   int(t) if t else 0,
        })
    return out
