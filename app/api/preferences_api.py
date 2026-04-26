"""Preferences API — auto-routing user preferences (v1.12+).

Stores admin/user-level preferences that influence how Arbiter chooses a
free-tier model when ``model="auto"`` (or no model) is requested:

  * ``priority``                 — "speed" | "quality" | "balanced"
  * ``prefer_providers``         — preferred provider names (boosts ranking)
  * ``avoid_providers``          — providers to skip during auto routing
  * ``<intent>_models_preference`` — explicit per-intent model order
       (intent ∈ code | reasoning | creative | vision | long_context | fast)
  * ``allow_paid_fallback``      — opt-in to paid Routeway fallback
                                   (only when no free model can serve the request)

The preferences live in the existing file-locked ``data/arbiter_state.json``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import state_store
from app.providers._free_tier_catalog import FREE_TIER_CATALOG, PAID_FALLBACK_CATALOG
from app.api.users_api import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/preferences", tags=["Preferences"])


class AutoRoutePreferences(BaseModel):
    """Schema for partial-update PUT body — every field optional."""
    priority: Optional[str] = Field(
        None, pattern="^(speed|quality|balanced)$",
        description="Auto-routing priority bias.",
    )
    prefer_providers: Optional[List[str]] = None
    avoid_providers: Optional[List[str]] = None
    code_models_preference: Optional[List[str]] = None
    reasoning_models_preference: Optional[List[str]] = None
    creative_models_preference: Optional[List[str]] = None
    vision_models_preference: Optional[List[str]] = None
    long_context_models_preference: Optional[List[str]] = None
    fast_models_preference: Optional[List[str]] = None
    allow_paid_fallback: Optional[bool] = None


@router.get("/auto-route")
async def get_auto_route_preferences() -> JSONResponse:
    """Return the current auto-routing preferences plus available choices."""
    prefs = state_store.get_auto_route_preferences()

    # Provide UI hints: every known free model + paid fallback list.
    available_models: Dict[str, List[str]] = {
        provider: [s.id for s in specs]
        for provider, specs in FREE_TIER_CATALOG.items()
    }
    paid_models: Dict[str, List[str]] = {
        provider: [s.id for s in specs]
        for provider, specs in PAID_FALLBACK_CATALOG.items()
    }

    return JSONResponse({
        "preferences": prefs,
        "available_providers": list(FREE_TIER_CATALOG.keys()),
        "available_free_models": available_models,
        "available_paid_fallback_models": paid_models,
        "supported_priorities": ["speed", "quality", "balanced"],
        "supported_intents": [
            "code", "reasoning", "creative",
            "vision", "long_context", "fast",
        ],
    })


@router.put("/auto-route", dependencies=[Depends(require_admin)])
async def update_auto_route_preferences(
    body: AutoRoutePreferences,
    request: Request,
) -> JSONResponse:
    """Partial update — only fields explicitly set in the body are changed."""
    updates: Dict[str, Any] = {
        k: v for k, v in body.model_dump().items() if v is not None
    }
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No preference fields provided.",
        )
    try:
        merged = state_store.update_auto_route_preferences(updates)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    logger.info("auto-route preferences updated: %s", list(updates.keys()))
    return JSONResponse({"preferences": merged, "updated_fields": list(updates.keys())})


@router.post("/auto-route/reset", dependencies=[Depends(require_admin)])
async def reset_auto_route_preferences() -> JSONResponse:
    """Reset auto-routing preferences to factory defaults."""
    defaults = state_store.update_auto_route_preferences(
        {
            "priority": "balanced",
            "prefer_providers": [],
            "avoid_providers": [],
            "code_models_preference": [],
            "reasoning_models_preference": [],
            "creative_models_preference": [],
            "vision_models_preference": [],
            "long_context_models_preference": [],
            "fast_models_preference": [],
            "allow_paid_fallback": False,
        }
    )
    return JSONResponse({"preferences": defaults, "reset": True})
