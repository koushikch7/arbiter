"""
Announcements API — read-only for the dashboard banner, plus admin-only
post/delete endpoints so operators can publish major-change notices.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.users_api import require_admin
from app.services import announcements as ann_svc
from app.observability.persistent_log import (
    log_activity as _log_activity,
    resolve_actor as _resolve_actor,
    client_ip_of as _client_ip_of,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/announcements", tags=["Announcements"])


class CreateAnnouncementBody(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    body:  str = Field(..., min_length=3, max_length=2000)
    severity: str = Field("warning", description="info | warning | critical")
    impacted_providers: Optional[List[str]] = None
    impacted_endpoints: Optional[List[str]] = None
    action_required: Optional[str] = None
    docs_url: Optional[str] = None
    ttl_days: int = Field(3, ge=1, le=30)


@router.get("/active", summary="Active dashboard banner announcements")
async def list_active(request: Request) -> JSONResponse:
    """
    Public to any authenticated dashboard session. Returns up to ~50 entries
    sorted newest first, each with its computed list of impacted gateway
    tokens (resolved on read so the data is always current).
    """
    redis = request.app.state.redis
    items = await ann_svc.get_active_announcements(redis, include_impacted_tokens=True)
    return JSONResponse({"announcements": items, "count": len(items)})


@router.post(
    "",
    summary="Post a major-change announcement",
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def create_announcement(body: CreateAnnouncementBody, request: Request) -> JSONResponse:
    redis = request.app.state.redis
    record = await ann_svc.post_announcement(
        redis,
        title=body.title,
        body=body.body,
        severity=body.severity,
        impacted_providers=body.impacted_providers,
        impacted_endpoints=body.impacted_endpoints,
        action_required=body.action_required,
        docs_url=body.docs_url,
        ttl_days=body.ttl_days,
    )

    try:
        email, role = _resolve_actor(request)
        await _log_activity(
            actor_email=email, actor_role=role,
            action="announcement.create",
            target=f"announcement:{record['id']}",
            after={
                "title":              record["title"],
                "severity":           record["severity"],
                "impacted_providers": record["impacted_providers"],
                "ttl_days":           record["ttl_days"],
            },
            request_ip=_client_ip_of(request),
        )
    except Exception:
        pass

    return JSONResponse(status_code=201, content=record)


@router.delete(
    "/{ann_id}",
    summary="Retract an active announcement",
    dependencies=[Depends(require_admin)],
)
async def delete_announcement(ann_id: str, request: Request) -> JSONResponse:
    redis = request.app.state.redis
    ok = await ann_svc.delete_announcement(redis, ann_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Announcement not found")

    try:
        email, role = _resolve_actor(request)
        await _log_activity(
            actor_email=email, actor_role=role,
            action="announcement.delete",
            target=f"announcement:{ann_id}",
            before={"existed": True}, after={"existed": False},
            request_ip=_client_ip_of(request),
        )
    except Exception:
        pass

    return JSONResponse({"success": True, "deleted": ann_id})
