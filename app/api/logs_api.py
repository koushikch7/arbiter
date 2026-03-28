"""
Real-time log viewer API — access, filter, and stream application logs from the UI.

The LogBuffer is a Python logging.Handler that keeps the last MAX_RECORDS log
records in a thread-safe deque.  It is attached to the root logger at startup.

Routes
──────
GET  /logs                  Serve the logs HTML page
GET  /logs/records          Fetch log records (filterable, sortable, pageable)
GET  /logs/loggers          List all unique logger names seen so far
DELETE /logs/clear          Clear the in-memory buffer
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Logs"])

# ---------------------------------------------------------------------------
# In-memory circular log buffer
# ---------------------------------------------------------------------------

MAX_RECORDS = 5_000   # keep last N records; oldest are evicted automatically

_LEVEL_NUM = {
    "DEBUG":    logging.DEBUG,
    "INFO":     logging.INFO,
    "WARNING":  logging.WARNING,
    "ERROR":    logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class LogBuffer(logging.Handler):
    """Thread-safe circular buffer that stores the last MAX_RECORDS log records."""

    def __init__(self, maxlen: int = MAX_RECORDS):
        super().__init__(level=logging.DEBUG)
        self._records: Deque[Dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._lock:
            self._seq += 1
            self._records.append({
                "seq":       self._seq,
                "ts":        record.created,          # Unix float
                "ts_iso":    time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
                ) + f".{int((record.created % 1) * 1000):03d}Z",
                "level":     record.levelname,
                "logger":    record.name,
                "message":   msg,
                "lineno":    record.lineno,
                "filename":  record.filename,
            })

    def get_records(
        self,
        level: Optional[str] = None,
        logger_prefix: Optional[str] = None,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        q: Optional[str] = None,
        tail: int = 0,
        limit: int = 200,
        newest_first: bool = True,
    ) -> List[Dict]:
        min_level = _LEVEL_NUM.get((level or "").upper(), logging.DEBUG)
        with self._lock:
            items = list(self._records)

        # Filter
        result = []
        for r in items:
            if _LEVEL_NUM.get(r["level"], 0) < min_level:
                continue
            if logger_prefix and not r["logger"].startswith(logger_prefix):
                continue
            if since_ts and r["ts"] < since_ts:
                continue
            if until_ts and r["ts"] > until_ts:
                continue
            if q and q.lower() not in r["message"].lower():
                continue
            result.append(r)

        # Tail takes last N after filtering
        if tail > 0:
            result = result[-tail:]

        # Sort
        result.sort(key=lambda x: x["seq"], reverse=newest_first)

        return result[:limit]

    def get_loggers(self) -> List[str]:
        with self._lock:
            return sorted({r["logger"] for r in self._records})

    def clear(self) -> int:
        with self._lock:
            n = len(self._records)
            self._records.clear()
            return n

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._records)


# Module-level singleton — registered in main.py lifespan
log_buffer = LogBuffer()

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log_buffer.setFormatter(_formatter)


# ---------------------------------------------------------------------------
# HTML page route
# ---------------------------------------------------------------------------

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "static",
)
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


@router.get("/logs", response_class=HTMLResponse, summary="Log viewer UI")
async def logs_page() -> HTMLResponse:
    path = os.path.join(_STATIC_DIR, "logs.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE)
    except FileNotFoundError:
        return HTMLResponse("<h1>Logs page not found</h1>", status_code=404)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@router.get("/logs/records", summary="Fetch log records")
async def get_log_records(
    level:        Optional[str] = None,
    logger_name:  Optional[str] = None,
    since:        Optional[float] = None,
    until:        Optional[float] = None,
    q:            Optional[str] = None,
    tail:         int = 0,
    limit:        int = 200,
    newest_first: bool = True,
) -> JSONResponse:
    """
    Query the in-memory log buffer.

    - **level**: minimum level — DEBUG | INFO | WARNING | ERROR | CRITICAL
    - **logger_name**: filter by logger name prefix (e.g. `app.api`)
    - **since** / **until**: Unix epoch float bounds
    - **q**: full-text search in the formatted message
    - **tail**: return only the last N records (after other filters)
    - **limit**: max records returned (default 200, max 5000)
    - **newest_first**: sort newest-first (default true)
    """
    limit = max(1, min(limit, MAX_RECORDS))
    records = log_buffer.get_records(
        level=level,
        logger_prefix=logger_name,
        since_ts=since,
        until_ts=until,
        q=q,
        tail=tail,
        limit=limit,
        newest_first=newest_first,
    )
    return JSONResponse(content={
        "total_buffered": log_buffer.total,
        "returned":       len(records),
        "records":        records,
    })


@router.get("/logs/loggers", summary="List all logger names seen")
async def list_loggers() -> JSONResponse:
    return JSONResponse(content={"loggers": log_buffer.get_loggers()})


@router.delete("/logs/clear", summary="Clear the in-memory log buffer")
async def clear_logs() -> JSONResponse:
    n = log_buffer.clear()
    return JSONResponse(content={"cleared": n})
