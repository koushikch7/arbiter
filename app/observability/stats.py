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
_DAILY_TTL_SECONDS = 90 * 24 * 3600

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

    # ── 5-min history bucket ───────────────────────────────────────────
    bucket = (int(time.time()) // 300) * 300
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests")
    await _incr(redis, f"arbiter:stats:history:{bucket}:success")

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
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests")
    await _incr(redis, f"arbiter:stats:history:{bucket}:errors")

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
    await _incr(redis, f"arbiter:stats:history:{bucket}:requests")
    await _incr(redis, f"arbiter:stats:history:{bucket}:success")
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
