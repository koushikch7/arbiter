"""UI error reporter — receives JS errors from the frontend and stores them
to the persistent errors log (180-day retention) for triage.

v1.20 addition. Light-weight: same persistent-log mechanism the gateway
uses internally, gated by per-IP rate limit so a misbehaving page can't
DoS the log dir.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.observability import persistent_log

router = APIRouter(tags=["UI Errors"])


class UIErrorPayload(BaseModel):
    kind: str = Field(..., max_length=32, description="error | promise")
    msg: str = Field("", max_length=2000)
    src: str = Field("", max_length=512)
    line: int = 0
    col: int = 0
    stack: str = Field("", max_length=4000)
    ua: str = Field("", max_length=256)
    page: str = Field("", max_length=256)


_LAST_BY_IP: "OrderedDict[str, float]" = OrderedDict()
_LAST_BY_IP_MAX = 2048
_MIN_INTERVAL_SEC = 2  # 1 error report per 2 seconds per IP


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, honouring the X-Forwarded-For chain set by
    nginx / Cloudflare (so the limiter buckets per real user, not per proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "?"


def _fallback_rate_limited(ip: str, now: float) -> bool:
    """Bounded in-process limiter for when Redis is down."""
    last = _LAST_BY_IP.get(ip, 0.0)
    if now - last < _MIN_INTERVAL_SEC:
        return True
    _LAST_BY_IP[ip] = now
    _LAST_BY_IP.move_to_end(ip)
    # Evict oldest entries so the dict can never grow without bound.
    while len(_LAST_BY_IP) > _LAST_BY_IP_MAX:
        _LAST_BY_IP.popitem(last=False)
    return False


@router.post(
    "/api/ui-error",
    summary="Report a frontend JS error",
    description=(
        "Endpoint for the frontend JS error reporter shim to POST runtime "
        "errors and unhandled-promise rejections. Records to the persistent "
        "errors log (180-day retention). Rate-limited to 1/2s per source IP."
    ),
)
async def report_ui_error(payload: UIErrorPayload, request: Request) -> JSONResponse:
    ip = _client_ip(request)
    now = time.time()

    # Primary limiter: Redis (works across workers/replicas). The key
    # self-evicts after the interval via EX, so there's no unbounded growth.
    redis = getattr(request.app.state, "redis", None)
    limited = False
    if redis is not None:
        try:
            rl_key = f"arbiter:ui_error:rl:{ip}"
            count = await redis.incr(rl_key)
            if count == 1:
                await redis.expire(rl_key, _MIN_INTERVAL_SEC)
            limited = count > 1
        except Exception:
            # Redis trouble → fall back to the bounded in-process limiter.
            limited = _fallback_rate_limited(ip, now)
    else:
        limited = _fallback_rate_limited(ip, now)

    if limited:
        return JSONResponse({"ok": False, "reason": "rate_limited"}, status_code=429)

    rec = {
        "kind":  payload.kind,
        "msg":   payload.msg,
        "src":   payload.src,
        "line":  payload.line,
        "col":   payload.col,
        "stack": payload.stack,
        "ua":    payload.ua,
        "page":  payload.page,
        "ip":    ip,
    }
    try:
        persistent_log.write_error(rec)
    except Exception:
        return JSONResponse({"ok": False}, status_code=500)
    return JSONResponse({"ok": True})
