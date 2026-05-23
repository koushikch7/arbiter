"""
Major-change announcement service.

When a breaking or otherwise significant change is rolled out, an announcement
is posted that downstream consumers see as a dashboard banner for the first
``ttl_days`` days. The banner also names the providers / gateways most likely
to be impacted, computed from recent usage stats so the operator can see at a
glance whose integrations need attention.

Storage
-------
Each announcement is one JSON document at ``arbiter:announcement:{id}`` with a
TTL of ``ttl_days * 86400``. The index of *active* announcement IDs is the
sorted-set ``arbiter:announcements:active`` (score = creation timestamp).
Expired IDs are pruned lazily on read.

This module is intentionally read-mostly: the dashboard polls
``get_active_announcements`` on every page load (cheap — typically 0-3 entries)
and dismissals are tracked client-side in localStorage so that one operator's
dismissal does not hide the banner from another operator.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_KEY_PREFIX = "arbiter:announcement:"
_KEY_INDEX  = "arbiter:announcements:active"

# Default banner lifetime — quote from product owner:
# "notified in the dashboard for first 3 days of the major changes"
DEFAULT_TTL_DAYS = 3

_VALID_SEVERITIES = ("info", "warning", "critical")


async def post_announcement(
    redis,
    *,
    title: str,
    body: str,
    severity: str = "warning",
    impacted_providers: Iterable[str] | None = None,
    impacted_endpoints: Iterable[str] | None = None,
    action_required: str | None = None,
    docs_url: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict:
    """
    Create a new announcement and store it with the configured TTL.

    Returns the stored announcement dict (including its generated ``id``).
    Impacted gateway tokens are resolved on read, not on write, so the list
    stays current as usage shifts within the banner window.
    """
    if severity not in _VALID_SEVERITIES:
        severity = "warning"
    ttl_days = max(1, min(int(ttl_days or DEFAULT_TTL_DAYS), 30))

    ann_id = f"ann_{int(time.time())}_{secrets.token_hex(3)}"
    now    = time.time()
    record = {
        "id":                 ann_id,
        "title":              title.strip()[:200],
        "body":               body.strip()[:2000],
        "severity":           severity,
        "impacted_providers": sorted(set(p.lower() for p in (impacted_providers or []) if p)),
        "impacted_endpoints": sorted(set(impacted_endpoints or [])),
        "action_required":    (action_required or "").strip()[:500] or None,
        "docs_url":           (docs_url or "").strip()[:500] or None,
        "created_at":         now,
        "expires_at":         now + ttl_days * 86400,
        "ttl_days":           ttl_days,
    }

    ttl_seconds = ttl_days * 86400
    try:
        await redis.set(_KEY_PREFIX + ann_id, json.dumps(record), ex=ttl_seconds)
        # zadd: score is creation time so we can show newest first
        await redis.zadd(_KEY_INDEX, {ann_id: now})
        # Cap the index at 50 entries to keep it bounded even with frequent posts
        await redis.zremrangebyrank(_KEY_INDEX, 0, -51)
    except Exception as exc:
        logger.warning("Failed to persist announcement %s: %s", ann_id, exc)

    logger.info("Announcement posted: id=%s severity=%s title=%r", ann_id, severity, record["title"])
    return record


async def _resolve_impacted_tokens(redis, providers: list[str]) -> list[dict]:
    """
    For each provider in *providers*, find gateway tokens that have called it
    in the last 30 days. Returns a list of {token_id, token_name, providers}
    deduplicated by token id, capped at 25 entries.
    """
    if not providers:
        return []

    # Stats keys follow: arbiter:stats:token:{tid}:provider:{p}:requests
    # We scan tokens once and cross-check each impacted provider.
    impacted: dict[str, dict] = {}
    try:
        async for key in redis.scan_iter("arbiter:stats:token:*:provider:*:requests"):
            try:
                key_str = key if isinstance(key, str) else key.decode()
            except Exception:
                continue
            parts = key_str.split(":")
            # ['arbiter','stats','token','{tid}','provider','{p}','requests']
            if len(parts) != 7:
                continue
            tid = parts[3]
            p   = parts[5].lower()
            if p not in providers:
                continue
            try:
                count = int(await redis.get(key) or 0)
            except Exception:
                count = 0
            if count <= 0:
                continue
            entry = impacted.setdefault(tid, {"token_id": tid, "providers": [], "requests": 0})
            if p not in entry["providers"]:
                entry["providers"].append(p)
            entry["requests"] += count
            if len(impacted) >= 200:
                break
    except Exception as exc:
        logger.debug("Could not resolve impacted tokens: %s", exc)
        return []

    # Annotate with token display name if available
    try:
        raw = await redis.get("arbiter:gateway:tokens")
        token_meta = {t["id"]: t for t in (json.loads(raw) if raw else [])}
        for tid, entry in impacted.items():
            entry["token_name"] = (token_meta.get(tid) or {}).get("name") or tid
    except Exception:
        for entry in impacted.values():
            entry.setdefault("token_name", entry["token_id"])

    ranked = sorted(impacted.values(), key=lambda x: -x["requests"])[:25]
    return ranked


async def get_active_announcements(redis, *, include_impacted_tokens: bool = True) -> list[dict]:
    """
    Return active announcements (newest first), lazily pruning any whose
    underlying record has expired out of Redis.
    """
    try:
        ids = await redis.zrevrange(_KEY_INDEX, 0, 49)
    except Exception as exc:
        logger.debug("Cannot read announcement index: %s", exc)
        return []

    out: list[dict] = []
    stale: list[str] = []
    for raw_id in (ids or []):
        ann_id = raw_id if isinstance(raw_id, str) else raw_id.decode()
        try:
            raw = await redis.get(_KEY_PREFIX + ann_id)
        except Exception:
            continue
        if not raw:
            stale.append(ann_id)
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            stale.append(ann_id)
            continue
        if rec.get("expires_at", 0) < time.time():
            stale.append(ann_id)
            continue
        if include_impacted_tokens and rec.get("impacted_providers"):
            rec["impacted_tokens"] = await _resolve_impacted_tokens(
                redis, rec["impacted_providers"]
            )
        else:
            rec.setdefault("impacted_tokens", [])
        out.append(rec)

    if stale:
        try:
            await redis.zrem(_KEY_INDEX, *stale)
        except Exception:
            pass
    return out


async def delete_announcement(redis, ann_id: str) -> bool:
    """Manually retract an announcement before its TTL elapses."""
    try:
        await redis.delete(_KEY_PREFIX + ann_id)
        await redis.zrem(_KEY_INDEX, ann_id)
        return True
    except Exception as exc:
        logger.warning("Failed to delete announcement %s: %s", ann_id, exc)
        return False
