"""
Dynamic gateway API token management — create/revoke/update tokens without restart.

Tokens are stored in Redis at key `arbiter:gateway:tokens` as a JSON list and
merged with any static keys defined via the GATEWAY_API_KEYS env var.

Routes
------
GET    /api/gateway/tokens                  List all tokens (key masked)
POST   /api/gateway/tokens                  Create a new token
DELETE /api/gateway/tokens/{id}             Revoke/delete a token
PATCH  /api/gateway/tokens/{id}             Update token (name, expires_at, active)
POST   /api/gateway/tokens/{id}/regenerate  Regenerate the key for a token
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.api.users_api import require_admin
from app.observability import stats as obs_stats
from app.observability.persistent_log import (
    log_activity as _log_activity,
    resolve_actor as _resolve_actor,
    client_ip_of as _client_ip_of,
)


async def _audit(request: Request, action: str, target: str,
                 before=None, after=None, note: str | None = None) -> None:
    try:
        email, role = _resolve_actor(request)
        await _log_activity(
            actor_email=email, actor_role=role,
            action=action, target=target,
            before=before, after=after,
            request_ip=_client_ip_of(request), note=note,
        )
    except Exception:
        pass

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/gateway",
    tags=["Gateway Tokens"],
    dependencies=[Depends(require_admin)],
)

_REDIS_TOKENS_KEY = "arbiter:gateway:tokens"


# ── Pydantic models ───────────────────────────────────────────────────────────


class CreateTokenBody(BaseModel):
    name: str
    expires_at: Optional[float] = None
    routing_policy: str = "auto"                    # "auto" | "restricted" | "preferred"
    allowed_models: Optional[List[str]] = None      # for restricted/preferred modes
    blocked_models: Optional[List[str]] = None      # models to never use


class UpdateTokenBody(BaseModel):
    name: Optional[str] = None
    expires_at: Optional[float] = Field(None)
    active: Optional[bool] = None
    routing_policy: Optional[str] = None
    allowed_models: Optional[List[str]] = None
    blocked_models: Optional[List[str]] = None


# ── Helper functions ──────────────────────────────────────────────────────────


async def _load_tokens(redis) -> List[dict]:
    """Load token list from Redis."""
    raw = await redis.get(_REDIS_TOKENS_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to decode gateway tokens from Redis; returning empty list.")
        return []


async def _save_tokens(redis, tokens: List[dict]) -> None:
    """Save token list to Redis."""
    await redis.set(_REDIS_TOKENS_KEY, json.dumps(tokens))


def _generate_key() -> str:
    """Generate a secure API key."""
    return "arbiter-sk-" + secrets.token_hex(32)


def _mask_key(key: str) -> str:
    """Mask key for display, showing only the prefix and last 4 chars."""
    if len(key) <= 14:
        return key[:10] + "****"
    return key[:14] + "****" + key[-4:]


async def _sync_app_tokens(request: Request, tokens: List[dict]) -> None:
    """Update app.state.gateway_tokens with active, non-expired tokens."""
    now = time.time()
    active_keys: set[str] = set()
    meta: dict[str, dict] = {}
    for t in tokens:
        if t.get("active", True):
            if t.get("expires_at") is None or t["expires_at"] > now:
                key = t["key"]
                active_keys.add(key)
                meta[key] = {
                    "id": t["id"],
                    "name": t.get("name", ""),
                    "routing_policy": t.get("routing_policy", "auto"),
                    "allowed_models": t.get("allowed_models"),
                    "blocked_models": t.get("blocked_models"),
                }
    # Merge with env-var keys
    env_keys = settings.get_gateway_api_keys()
    for k in env_keys:
        active_keys.add(k)
        meta.setdefault(k, {"id": "env", "name": "env-var"})
    request.app.state.gateway_tokens = active_keys
    request.app.state.gateway_token_meta = meta


# ── Startup helper ────────────────────────────────────────────────────────────


async def load_gateway_tokens_to_state(app) -> None:
    """
    Populate app.state.gateway_tokens from Redis + env vars.
    Call this at application startup.
    """
    redis = app.state.redis
    tokens = await _load_tokens(redis)
    now = time.time()
    active_keys: set[str] = set()
    meta: dict[str, dict] = {}
    for t in tokens:
        if t.get("active", True):
            if t.get("expires_at") is None or t["expires_at"] > now:
                key = t["key"]
                active_keys.add(key)
                meta[key] = {
                    "id": t["id"],
                    "name": t.get("name", ""),
                    "routing_policy": t.get("routing_policy", "auto"),
                    "allowed_models": t.get("allowed_models"),
                    "blocked_models": t.get("blocked_models"),
                }
    env_keys = settings.get_gateway_api_keys()
    for k in env_keys:
        active_keys.add(k)
        meta.setdefault(k, {"id": "env", "name": "env-var"})
    app.state.gateway_tokens = active_keys
    app.state.gateway_token_meta = meta
    logger.info(
        "Gateway tokens loaded: %d from Redis, %d from env vars (%d total active).",
        sum(1 for t in tokens if t.get("active", True)),
        len(env_keys),
        len(active_keys),
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/tokens")
async def list_tokens(request: Request):
    """
    List all gateway tokens with live request counters merged in.

    Each token row includes ``request_count``, ``success_count``, ``error_count``,
    ``tokens_used`` and ``last_used_at`` sourced from Redis. Also includes
    ``env_keys_count`` showing how many keys are configured via environment
    variables (those cannot be managed here but their traffic shows up under
    the synthetic ``env`` token id).
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    masked_tokens = []
    for t in tokens:
        entry = dict(t)
        entry["key"] = _mask_key(t["key"])
        live = await obs_stats.get_token_summary(redis, t["id"])
        entry["request_count"] = live["requests"]
        entry["success_count"] = live["success"]
        entry["error_count"]   = live["errors"]
        entry["tokens_used"]   = live["tokens"]
        if live["last_used_at"]:
            entry["last_used_at"] = live["last_used_at"]
        masked_tokens.append(entry)

    env_keys = settings.get_gateway_api_keys()
    env_summary = await obs_stats.get_token_summary(redis, "env") if env_keys else None

    return {
        "tokens": masked_tokens,
        "env_keys_count": len(env_keys),
        "env_traffic": env_summary,
    }


@router.get("/tokens/{token_id}/stats")
async def token_stats(token_id: str, request: Request):
    """
    Detailed analytics for a single gateway token.

    Returns lifetime counters plus per-day, per-provider, and per-model
    breakdowns so the UI can render a usage chart.
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)
    token = next((t for t in tokens if t["id"] == token_id), None)
    if token is None and token_id != "env":
        raise HTTPException(404, f"Token '{token_id}' not found.")

    summary = await obs_stats.get_token_summary(redis, token_id)

    # Per-provider
    by_provider: dict[str, int] = {}
    try:
        keys = await redis.keys(
            f"arbiter:stats:token:{obs_stats._safe(token_id, 40)}:provider:*:requests"
        )
        for k in keys:
            parts = k.split(":")
            if len(parts) >= 7:
                pname = parts[5]
                by_provider[pname] = await obs_stats.get_int(redis, k)
    except Exception:
        pass

    # Per-model
    by_model: dict[str, int] = {}
    try:
        keys = await redis.keys(
            f"arbiter:stats:token:{obs_stats._safe(token_id, 40)}:model:*:requests"
        )
        for k in keys:
            parts = k.split(":")
            if len(parts) >= 7:
                mname = parts[5]
                by_model[mname] = await obs_stats.get_int(redis, k)
    except Exception:
        pass

    # Last 30 days
    history: list[dict] = []
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    today = _dt.now(_tz.utc).date()
    for i in range(30):
        d = (today - _td(days=29 - i)).strftime("%Y-%m-%d")
        req = await obs_stats.get_int(
            redis, f"arbiter:stats:day:{d}:token:{obs_stats._safe(token_id, 40)}:requests")
        tok = await obs_stats.get_int(
            redis, f"arbiter:stats:day:{d}:token:{obs_stats._safe(token_id, 40)}:tokens")
        history.append({"date": d, "requests": req, "tokens": tok})

    masked = dict(token) if token else {"id": "env", "name": "env-var"}
    if "key" in masked:
        masked["key"] = _mask_key(masked["key"])

    return {
        "token": masked,
        "summary": summary,
        "by_provider": by_provider,
        "by_model": by_model,
        "history_30d": history,
    }


@router.post("/tokens", status_code=201)
async def create_token(body: CreateTokenBody, request: Request):
    """
    Create a new gateway API token.

    The plaintext key is returned only in this response — store it securely.
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    token_id = "gwtk_" + secrets.token_hex(6)
    key = _generate_key()
    now = time.time()

    new_token = {
        "id": token_id,
        "name": body.name,
        "key": key,
        "created_at": now,
        "expires_at": body.expires_at,
        "last_used_at": None,
        "request_count": 0,
        "active": True,
        "routing_policy": body.routing_policy,
        "allowed_models": body.allowed_models,
        "blocked_models": body.blocked_models,
    }

    tokens.append(new_token)
    await _save_tokens(redis, tokens)
    await _sync_app_tokens(request, tokens)

    logger.info("Gateway token created: id=%s name=%r", token_id, body.name)
    await _audit(
        request, action="gateway_token.create",
        target=f"gateway_token:{token_id}",
        after={
            "name": body.name,
            "routing_policy": body.routing_policy,
            "allowed_models": body.allowed_models,
            "blocked_models": body.blocked_models,
            "expires_at": body.expires_at,
        },
    )

    # Return full token including plaintext key (only time it is shown)
    return new_token


@router.delete("/tokens/{token_id}", status_code=200)
async def delete_token(token_id: str, request: Request):
    """
    Revoke and permanently delete a gateway token.
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    original_len = len(tokens)
    tokens = [t for t in tokens if t["id"] != token_id]

    if len(tokens) == original_len:
        raise HTTPException(status_code=404, detail=f"Token '{token_id}' not found.")

    await _save_tokens(redis, tokens)
    await _sync_app_tokens(request, tokens)

    logger.info("Gateway token deleted: id=%s", token_id)
    await _audit(
        request, action="gateway_token.delete",
        target=f"gateway_token:{token_id}",
        before={"existed": True}, after={"existed": False},
    )

    return {"detail": f"Token '{token_id}' has been revoked and deleted."}


@router.patch("/tokens/{token_id}")
async def update_token(token_id: str, body: UpdateTokenBody, request: Request):
    """
    Update a gateway token's name, expiry, or active status.

    Setting `active` to false immediately removes the key from the live auth set.
    Setting `active` to true re-adds it (if not expired).
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    token = next((t for t in tokens if t["id"] == token_id), None)
    if token is None:
        raise HTTPException(status_code=404, detail=f"Token '{token_id}' not found.")

    _before = {
        "name": token.get("name"),
        "active": token.get("active"),
        "routing_policy": token.get("routing_policy"),
        "allowed_models": token.get("allowed_models"),
        "blocked_models": token.get("blocked_models"),
        "expires_at": token.get("expires_at"),
    }

    if body.name is not None:
        token["name"] = body.name
    if body.expires_at is not False:  # Field(None) means it can be explicitly set to null
        token["expires_at"] = body.expires_at
    if body.active is not None:
        token["active"] = body.active
    if body.routing_policy is not None:
        if body.routing_policy not in ("auto", "restricted", "preferred"):
            raise HTTPException(422, "routing_policy must be auto, restricted, or preferred")
        token["routing_policy"] = body.routing_policy
    if body.allowed_models is not None:
        token["allowed_models"] = body.allowed_models
    if body.blocked_models is not None:
        token["blocked_models"] = body.blocked_models

    await _save_tokens(redis, tokens)
    await _sync_app_tokens(request, tokens)

    logger.info(
        "Gateway token updated: id=%s updates=%r",
        token_id,
        body.model_dump(exclude_none=True),
    )
    await _audit(
        request, action="gateway_token.update",
        target=f"gateway_token:{token_id}",
        before=_before,
        after={
            "name": token.get("name"),
            "active": token.get("active"),
            "routing_policy": token.get("routing_policy"),
            "allowed_models": token.get("allowed_models"),
            "blocked_models": token.get("blocked_models"),
            "expires_at": token.get("expires_at"),
        },
    )

    result = dict(token)
    result["key"] = _mask_key(token["key"])
    return result


@router.post("/tokens/{token_id}/regenerate")
async def regenerate_token(token_id: str, request: Request):
    """
    Regenerate the key for an existing token.

    The old key is immediately invalidated. The new plaintext key is returned
    only in this response — store it securely.
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    token = next((t for t in tokens if t["id"] == token_id), None)
    if token is None:
        raise HTTPException(status_code=404, detail=f"Token '{token_id}' not found.")

    old_key = token["key"]
    new_key = _generate_key()
    token["key"] = new_key

    await _save_tokens(redis, tokens)

    # Remove old key and add new key to live auth set
    current_tokens: set[str] = getattr(request.app.state, "gateway_tokens", set())
    current_tokens.discard(old_key)
    if token.get("active", True):
        expires_at = token.get("expires_at")
        if expires_at is None or expires_at > time.time():
            current_tokens.add(new_key)
    request.app.state.gateway_tokens = current_tokens

    logger.info("Gateway token key regenerated: id=%s", token_id)
    await _audit(
        request, action="gateway_token.regenerate",
        target=f"gateway_token:{token_id}",
        before={"key": old_key}, after={"key": new_key},
    )

    return {
        "id": token_id,
        "key": new_key,
        "detail": "Key regenerated successfully. Store the new key securely — it will not be shown again.",
    }
