"""
State store for runtime data that must survive restarts.

Everything here is persisted to ``data/arbiter_state.json`` (NOT Redis).
This decision was made deliberately — Redis caching caused stale-state
issues in practice, and these records are small, rarely written, and
don't need distribution across nodes.

Schema
------
::

    {
        "version": 1,
        "users": [
            {
                "email": "admin@example.com",
                "name": "Admin User",
                "picture": "https://...",
                "status": "approved" | "pending" | "rejected",
                "is_admin": true,
                "created_at": 1735689600.0,
                "last_login": 1735689600.0 | null,
                "session_version": 1
            }
        ],
        "custom_providers": [
            {
                "name": "deepseek",
                "label": "DeepSeek (custom)",
                "template": "deepseek" | "custom",
                "base_url": "https://api.deepseek.com/v1",
                "auth_header": "Authorization",
                "auth_prefix": "Bearer ",
                "models": ["deepseek-chat", ...],
                "max_context": 128000,
                "api_key_ref": "CUSTOM_PROVIDER_DEEPSEEK_KEY"  # env var name
            }
        ],
        "models": {
            "<provider>": {
                "<model_id>": {
                    "enabled": true | false,
                    "free": true | false | null,
                    "context": int | null,
                    "discovered_at": float,
                    "discovered": true | false   # True = came from fetch_models
                }
            }
        }
    }

Concurrency
-----------
All writes go through a ``filelock.FileLock`` so that parallel workers
never clobber each other. Writes are atomic (temp file + rename).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import RLock
from typing import Any

try:
    # filelock is lightweight and cross-platform
    from filelock import FileLock  # type: ignore
    _HAS_FILELOCK = True
except ImportError:  # pragma: no cover — added to requirements.txt
    _HAS_FILELOCK = False
    FileLock = None  # type: ignore

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR     = _PROJECT_ROOT / "data"
_STATE_FILE   = _DATA_DIR / "arbiter_state.json"
_LOCK_FILE    = _DATA_DIR / "arbiter_state.json.lock"

_MEMORY_LOCK = RLock()  # Backup for environments without filelock

_DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "users": [],
    "custom_providers": [],
    "models": {},
    "auto_route_preferences": {
        # priority: "speed" | "quality" | "balanced"
        "priority": "balanced",
        # ordered list of provider names — boost in scoring (top first).
        "prefer_providers": [],
        # zeroed-out providers; auto-router skips these entirely.
        "avoid_providers": [],
        # capability-specific overrides.  Each is an ordered list of model IDs
        # (highest preference first).  Empty list = use catalog defaults.
        "code_models_preference": [],
        "reasoning_models_preference": [],
        "creative_models_preference": [],
        "vision_models_preference": [],
        "long_context_models_preference": [],
        "fast_models_preference": [],
        # When the request explicitly opts in (metadata.opt_in_paid=true) this
        # flag controls whether paid fallback models are eligible for auto.
        # Default: false (free-only).
        "allow_paid_fallback": False,
    },
}


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _acquire_lock():
    """Return a context manager guarding concurrent writes."""
    _ensure_data_dir()
    if _HAS_FILELOCK and FileLock is not None:
        return FileLock(str(_LOCK_FILE), timeout=10)
    return _MEMORY_LOCK


def _atomic_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically via a temp file + os.replace()."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def load_state() -> dict[str, Any]:
    """Load the state file; return a fresh default dict if missing/corrupt."""
    _ensure_data_dir()
    if not _STATE_FILE.exists():
        return json.loads(json.dumps(_DEFAULT_STATE))  # deep copy

    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        # Forward-compat: backfill missing keys
        for key, default in _DEFAULT_STATE.items():
            if key not in data:
                data[key] = json.loads(json.dumps(default))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "arbiter_state.json is corrupt or unreadable (%s). "
            "Returning default state \u2014 existing file NOT overwritten; "
            "please repair manually at %s",
            exc, _STATE_FILE,
        )
        return json.loads(json.dumps(_DEFAULT_STATE))


def save_state(state: dict[str, Any]) -> None:
    """Write the full state dict to disk atomically.

    NOTE: this does NOT acquire the lock — callers inside this module
    already hold ``_acquire_lock()`` around their read-modify-write cycle,
    and re-acquiring the same FileLock from the ASGI thread-pool can lead
    to sporadic timeouts under concurrency.  External callers must wrap
    their own lock using ``_acquire_lock()`` or use the high-level
    accessors below.
    """
    _atomic_write(_STATE_FILE, json.dumps(state, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# High-level accessors
# ---------------------------------------------------------------------------

# ── Users ────────────────────────────────────────────────────────────────────

def list_users() -> list[dict]:
    return load_state().get("users", [])


def get_user(email: str) -> dict | None:
    email = (email or "").strip().lower()
    for u in list_users():
        if u.get("email", "").lower() == email:
            return u
    return None


def upsert_user(email: str, *, name: str = "", picture: str = "",
                status: str | None = None, is_admin: bool | None = None) -> dict:
    """Create or update a user; returns the updated record."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")

    with _acquire_lock():
        state = load_state()
        users = state.setdefault("users", [])
        found = None
        for u in users:
            if u.get("email", "").lower() == email:
                found = u
                break

        now = time.time()
        if found is None:
            found = {
                "email": email,
                "name": name,
                "picture": picture,
                "status": status or "pending",
                "is_admin": bool(is_admin) if is_admin is not None else False,
                "created_at": now,
                "last_login": None,
                "session_version": 1,
            }
            users.append(found)
        else:
            if name:
                found["name"] = name
            if picture:
                found["picture"] = picture
            if status is not None:
                found["status"] = status
            if is_admin is not None:
                found["is_admin"] = bool(is_admin)

        save_state(state)
        return dict(found)


def set_user_status(email: str, status: str) -> dict | None:
    """Update user status; invalidates session by bumping session_version on reject."""
    if status not in ("approved", "pending", "rejected"):
        raise ValueError(f"invalid status: {status}")
    email = (email or "").strip().lower()

    with _acquire_lock():
        state = load_state()
        for u in state.get("users", []):
            if u.get("email", "").lower() == email:
                u["status"] = status
                # Bump session_version so middleware kicks the user out immediately
                if status in ("rejected", "pending"):
                    u["session_version"] = int(u.get("session_version", 1)) + 1
                save_state(state)
                return dict(u)
    return None


def record_user_login(email: str) -> None:
    email = (email or "").strip().lower()
    with _acquire_lock():
        state = load_state()
        for u in state.get("users", []):
            if u.get("email", "").lower() == email:
                u["last_login"] = time.time()
                save_state(state)
                return


def delete_user(email: str) -> bool:
    email = (email or "").strip().lower()
    with _acquire_lock():
        state = load_state()
        before = len(state.get("users", []))
        state["users"] = [u for u in state.get("users", [])
                          if u.get("email", "").lower() != email]
        changed = len(state["users"]) != before
        if changed:
            save_state(state)
        return changed


# ── Custom providers ─────────────────────────────────────────────────────────

def list_custom_providers() -> list[dict]:
    return load_state().get("custom_providers", [])


def get_custom_provider(name: str) -> dict | None:
    for p in list_custom_providers():
        if p.get("name") == name:
            return p
    return None


def upsert_custom_provider(provider: dict) -> dict:
    """Insert or update a custom provider by ``name``."""
    name = provider.get("name")
    if not name:
        raise ValueError("provider name is required")

    with _acquire_lock():
        state = load_state()
        providers = state.setdefault("custom_providers", [])
        found = False
        for i, p in enumerate(providers):
            if p.get("name") == name:
                providers[i] = provider
                found = True
                break
        if not found:
            providers.append(provider)
        save_state(state)
        return dict(provider)


def delete_custom_provider(name: str) -> bool:
    with _acquire_lock():
        state = load_state()
        before = len(state.get("custom_providers", []))
        state["custom_providers"] = [p for p in state.get("custom_providers", [])
                                     if p.get("name") != name]
        changed = len(state["custom_providers"]) != before
        if changed:
            save_state(state)
        return changed


# ── Model enable/disable ─────────────────────────────────────────────────────

def get_model_state(provider: str) -> dict[str, dict]:
    """Return the ``{model_id: {enabled, free, context, ...}}`` map for a provider."""
    return load_state().get("models", {}).get(provider, {})


def is_model_enabled(provider: str, model_id: str, *, default: bool = True) -> bool:
    """Check if a specific model is enabled. Defaults to True when unknown."""
    entry = get_model_state(provider).get(model_id)
    if entry is None:
        return default
    return bool(entry.get("enabled", True))


def set_model_enabled(provider: str, model_id: str, enabled: bool) -> None:
    with _acquire_lock():
        state = load_state()
        models = state.setdefault("models", {})
        pmap   = models.setdefault(provider, {})
        entry  = pmap.setdefault(model_id, {"enabled": True, "free": None,
                                            "context": None, "discovered": False})
        entry["enabled"] = bool(enabled)
        save_state(state)


def record_discovered_models(provider: str, models: list[dict]) -> None:
    """
    Merge a list of ``{id, context, free}`` dicts from ``fetch_models()`` into
    the state store. Existing enabled flags are preserved; only newly-seen
    models are added (defaulting to enabled=True).
    """
    now = time.time()
    with _acquire_lock():
        state = load_state()
        pmap = state.setdefault("models", {}).setdefault(provider, {})
        for m in models:
            mid = str(m.get("id", "")).strip()
            if not mid:
                continue
            existing = pmap.get(mid, {})
            pmap[mid] = {
                "enabled":       bool(existing.get("enabled", True)),
                "free":          m.get("free") if m.get("free") is not None else existing.get("free"),
                "context":       m.get("context") or existing.get("context"),
                "discovered":    True,
                "discovered_at": now,
            }
        save_state(state)


def filter_enabled_models(provider: str, model_ids: list[str]) -> list[str]:
    """Return only the enabled model IDs from the list, in the original order."""
    return [m for m in model_ids if is_model_enabled(provider, m)]


# ── Auto-route preferences ─────────────────────────────────────────────────

def get_auto_route_preferences() -> dict:
    """Return the current auto-route preferences (always populated)."""
    state = load_state()
    prefs = state.get("auto_route_preferences") or {}
    # Fill in any missing keys from defaults — forward-compat.
    defaults = _DEFAULT_STATE["auto_route_preferences"]
    return {**defaults, **prefs}


def update_auto_route_preferences(updates: dict) -> dict:
    """Merge *updates* into stored preferences; return the new dict."""
    valid_priorities = {"speed", "quality", "balanced"}
    cleaned: dict = {}
    for k, v in (updates or {}).items():
        if k == "priority":
            if v not in valid_priorities:
                raise ValueError(f"priority must be one of {valid_priorities}")
            cleaned[k] = v
        elif k in (
            "prefer_providers", "avoid_providers",
            "code_models_preference", "reasoning_models_preference",
            "creative_models_preference", "vision_models_preference",
            "long_context_models_preference", "fast_models_preference",
        ):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{k} must be a list of strings")
            # de-dupe while preserving order
            seen = set()
            cleaned[k] = [x for x in v if not (x in seen or seen.add(x))]
        elif k == "allow_paid_fallback":
            cleaned[k] = bool(v)
    if not cleaned:
        return get_auto_route_preferences()

    with _acquire_lock():
        state = load_state()
        prefs = state.get("auto_route_preferences") or {}
        prefs = {**_DEFAULT_STATE["auto_route_preferences"], **prefs, **cleaned}
        state["auto_route_preferences"] = prefs
        save_state(state)
        return prefs
