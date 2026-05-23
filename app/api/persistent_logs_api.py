"""
Persistent-log query API (v1.18.0).

Exposes read-only endpoints for the three 180-day log streams written by
``app.observability.persistent_log``:

  GET /api/logs/persistent/summary        — aggregated stats (N-day window)
  GET /api/logs/persistent/api            — paginated API-call records
  GET /api/logs/persistent/activity       — paginated admin activity records
  GET /api/logs/persistent/errors         — paginated error records

All endpoints are admin-only.  Records are returned newest-first.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.users_api import require_admin
from app.observability import persistent_log as _plog

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/logs/persistent",
    tags=["Persistent Logs"],
    dependencies=[Depends(require_admin)],
)


# ── helpers ─────────────────────────────────────────────────────────────────

async def _collect(stream: str, days: int, limit: int, offset: int) -> List[dict]:
    """Pull records from a log stream, apply offset+limit, return list."""
    records: List[dict] = []
    async for rec in _plog.iter_records(stream, days):
        records.append(rec)
    # iter_records yields oldest-first per file; reverse for newest-first UI
    records.reverse()
    return records[offset : offset + limit]


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get(
    "/summary",
    summary="Persistent log summary (N-day aggregation)",
    response_description="Aggregated counts and latency percentiles for the requested window.",
)
async def persistent_summary(
    days: int = Query(7, ge=1, le=180, description="How many days of history to aggregate"),
):
    """
    Return aggregated statistics across all three log streams for the last
    *days* days.  Includes API call totals, error rate, p50/p95 latency,
    calls-by-provider, calls-by-token, error categories, and activity
    change counts.  Expensive on large histories — defaults to 7 days.
    """
    summary = await _plog.summarise(days=days)
    return JSONResponse(summary)


@router.get(
    "/api",
    summary="Persistent API-call log records",
    response_description="Paginated gateway request records (newest first).",
)
async def persistent_api_logs(
    days:   int = Query(7,   ge=1, le=180,  description="History window in days"),
    limit:  int = Query(100, ge=1, le=1000, description="Max records to return"),
    offset: int = Query(0,   ge=0,          description="Pagination offset"),
):
    """
    Paginated view of ``data/logs/api/YYYY-MM-DD.jsonl``.  Each record
    contains: ``ts``, ``token_id``, ``method``, ``path``, ``model``,
    ``provider``, ``status_code``, ``latency_ms``, ``prompt_tokens``,
    ``completion_tokens``, ``cached``, ``client_ip``, ``error``.
    """
    records = await _collect("api", days, limit, offset)
    return JSONResponse({"stream": "api", "days": days, "count": len(records), "records": records})


@router.get(
    "/activity",
    summary="Admin activity audit log records",
    response_description="Paginated admin-mutation records (newest first).",
)
async def persistent_activity_logs(
    days:   int = Query(7,   ge=1, le=180,  description="History window in days"),
    limit:  int = Query(100, ge=1, le=1000, description="Max records to return"),
    offset: int = Query(0,   ge=0,          description="Pagination offset"),
):
    """
    Paginated view of ``data/logs/activity/YYYY-MM-DD.jsonl``.  Each record
    contains: ``ts``, ``actor_email``, ``actor_role``, ``action``,
    ``target``, ``before``, ``after``, ``note``, ``client_ip``, ``hmac``.
    The ``hmac`` field is a SHA-256 tag for tamper detection.
    """
    records = await _collect("activity", days, limit, offset)
    return JSONResponse({"stream": "activity", "days": days, "count": len(records), "records": records})


@router.get(
    "/errors",
    summary="Persistent error log records",
    response_description="Paginated structured error records (newest first).",
)
async def persistent_error_logs(
    days:   int = Query(7,   ge=1, le=180,  description="History window in days"),
    limit:  int = Query(100, ge=1, le=1000, description="Max records to return"),
    offset: int = Query(0,   ge=0,          description="Pagination offset"),
):
    """
    Paginated view of ``data/logs/errors/YYYY-MM-DD.jsonl``.  Each record
    contains: ``ts``, ``provider``, ``model``, ``category``, ``message``,
    ``token_id``, ``client_ip``.
    """
    records = await _collect("errors", days, limit, offset)
    return JSONResponse({"stream": "errors", "days": days, "count": len(records), "records": records})


@router.post(
    "/prune",
    summary="Force retention prune (delete logs older than 180 days)",
    response_description="Count of files deleted per stream.",
)
async def persistent_prune():
    """
    Manually trigger the janitor to delete log files older than the
    configured 180-day retention window.  The janitor also runs
    automatically every day at 03:00 UTC.
    """
    result = await _plog.prune_now()
    return JSONResponse(result)
