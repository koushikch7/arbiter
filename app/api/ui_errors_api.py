"""UI error reporter — receives JS errors from the frontend and stores them
to the persistent errors log (180-day retention) for triage.

v1.20 addition. Light-weight: same persistent-log mechanism the gateway
uses internally, gated by per-IP rate limit so a misbehaving page can't
DoS the log dir.
"""
from __future__ import annotations

import time
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


_LAST_BY_IP: dict[str, float] = {}
_MIN_INTERVAL_SEC = 2.0  # 1 error report per 2 seconds per IP


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
    ip = (
        request.client.host if request.client else "?"
    )
    now = time.time()
    last = _LAST_BY_IP.get(ip, 0)
    if now - last < _MIN_INTERVAL_SEC:
        return JSONResponse({"ok": False, "reason": "rate_limited"}, status_code=429)
    _LAST_BY_IP[ip] = now

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
