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


class UpdateTokenBody(BaseModel):
    name: Optional[str] = None
    expires_at: Optional[float] = Field(None)
    active: Optional[bool] = None


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
    for t in tokens:
        if t.get("active", True):
            if t.get("expires_at") is None or t["expires_at"] > now:
                active_keys.add(t["key"])
    # Merge with env-var keys
    env_keys = settings.get_gateway_api_keys()
    active_keys.update(env_keys)
    request.app.state.gateway_tokens = active_keys


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
    for t in tokens:
        if t.get("active", True):
            if t.get("expires_at") is None or t["expires_at"] > now:
                active_keys.add(t["key"])
    env_keys = settings.get_gateway_api_keys()
    active_keys.update(env_keys)
    app.state.gateway_tokens = active_keys
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
    List all gateway tokens.

    Keys are returned masked. Also includes `env_keys_count` showing how many
    keys are configured via environment variables.
    """
    redis = request.app.state.redis
    tokens = await _load_tokens(redis)

    masked_tokens = []
    for t in tokens:
        entry = dict(t)
        entry["key"] = _mask_key(t["key"])
        masked_tokens.append(entry)

    env_keys = settings.get_gateway_api_keys()

    return {
        "tokens": masked_tokens,
        "env_keys_count": len(env_keys),
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
    }

    tokens.append(new_token)
    await _save_tokens(redis, tokens)
    await _sync_app_tokens(request, tokens)

    logger.info("Gateway token created: id=%s name=%r", token_id, body.name)

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

    if body.name is not None:
        token["name"] = body.name
    if body.expires_at is not False:  # Field(None) means it can be explicitly set to null
        token["expires_at"] = body.expires_at
    if body.active is not None:
        token["active"] = body.active

    await _save_tokens(redis, tokens)
    await _sync_app_tokens(request, tokens)

    logger.info(
        "Gateway token updated: id=%s updates=%r",
        token_id,
        body.model_dump(exclude_none=True),
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

    return {
        "id": token_id,
        "key": new_key,
        "detail": "Key regenerated successfully. Store the new key securely — it will not be shown again.",
    }
