"""
Persistent 180-day file-system log store (v1.18.0).

Three log streams, each as daily-rotated JSONL files under ``data/logs/``:

  data/logs/api/YYYY-MM-DD.jsonl       — every gateway request (input/output)
  data/logs/activity/YYYY-MM-DD.jsonl  — admin actions (settings, keys, providers, tokens)
  data/logs/errors/YYYY-MM-DD.jsonl    — structured upstream + internal errors

Retention is enforced by a daily janitor that deletes files older than
``LOG_RETENTION_DAYS`` (default 180). Cleanup runs once a day at 03:00 UTC.

Why a separate module (not just Python logging)
-----------------------------------------------
The existing `logging` setup uses an **in-memory ring buffer** of 5,000 entries
which is lost on restart. Redis stores `arbiter:error_log` as a list capped at
50. Neither survives reboots beyond a few hours.

This module writes append-only JSONL the gateway can keep for the legally /
operationally required retention period (here 180 days), then summarise
weekly via the email report.

All writes are best-effort and fail-soft — if disk is full or the directory
is unwritable, the gateway keeps serving traffic; the offending line is
dropped after one warning per minute (rate-limited via `logging`).

Security
--------
* Bearer tokens, API keys, and password-like values are redacted by
  ``_sanitise()`` before write. Activity-log diff entries hash the full
  secret with SHA-256 and store only the first/last 4 chars + hash.
* Every activity record carries an HMAC tag signed with
  ``SESSION_SECRET_KEY`` so tampering with the on-disk file is detectable.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from app.config import settings

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────
LOG_RETENTION_DAYS = 180

_LOG_ROOT = Path(os.environ.get("ARBITER_LOG_DIR", "/app/data/logs"))
_API_DIR      = _LOG_ROOT / "api"
_ACTIVITY_DIR = _LOG_ROOT / "activity"
_ERROR_DIR    = _LOG_ROOT / "errors"

# Async write lock per stream — prevents interleaved bytes when two coroutines
# write concurrently. JSONL must have one record per line.
_LOCKS: dict[str, asyncio.Lock] = {}
_WARN_THROTTLE: dict[str, float] = {}

# Regexes for redacting secrets in free-text payloads
_RE_AUTH_HDR = re.compile(r"(authorization\s*[:=]\s*bearer\s+)(\S+)", re.I)
_RE_SK       = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})", re.I)
_RE_AIZA     = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")
_RE_HF       = re.compile(r"\bhf_[A-Za-z0-9]{20,}")
_RE_NVAPI    = re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}")
_RE_GSK      = re.compile(r"\bgsk_[A-Za-z0-9]{20,}")
_RE_CFUT     = re.compile(r"\bcfut_[A-Za-z0-9]{20,}")
_RE_ARBKEY   = re.compile(r"\barbiter-sk-[A-Za-z0-9]{20,}")

_SECRET_HEADERS = {"authorization", "x-api-key", "x-arbiter-token", "cookie"}


def _ensure_dirs() -> None:
    """Create log dirs idempotently. Best-effort: silent on failure."""
    for d in (_API_DIR, _ACTIVITY_DIR, _ERROR_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _warn_once(str(d), f"mkdir failed: {exc}")


def _warn_once(key: str, message: str) -> None:
    """Emit a logger warning at most once per minute per `key`."""
    now = time.time()
    if now - _WARN_THROTTLE.get(key, 0) > 60:
        _WARN_THROTTLE[key] = now
        logger.warning(message)


def _sanitise(value: Any) -> Any:
    """Recursively redact obvious secrets from arbitrary JSON-ish values."""
    if isinstance(value, str):
        s = value
        s = _RE_AUTH_HDR.sub(lambda m: m.group(1) + "***", s)
        s = _RE_SK.sub("sk-***", s)
        s = _RE_AIZA.sub("AIza***", s)
        s = _RE_HF.sub("hf_***", s)
        s = _RE_NVAPI.sub("nvapi-***", s)
        s = _RE_GSK.sub("gsk_***", s)
        s = _RE_CFUT.sub("cfut_***", s)
        s = _RE_ARBKEY.sub("arbiter-sk-***", s)
        return s
    if isinstance(value, dict):
        return {
            k: ("***" if isinstance(k, str) and k.lower() in _SECRET_HEADERS
                else _sanitise(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitise(v) for v in value]
    return value


def _secret_fingerprint(secret: str) -> str:
    """Compact reversible-only-by-rainbow-table fingerprint of a secret."""
    if not secret:
        return ""
    h = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()[:12]
    head = secret[:4] if len(secret) > 12 else "***"
    tail = secret[-4:] if len(secret) > 12 else "***"
    return f"{head}…{tail}#{h}"


def _hmac_tag(record: dict) -> str:
    """Sign an activity record so tampering is detectable."""
    key = (getattr(settings, "SESSION_SECRET_KEY", "") or "arbiter-default").encode()
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()[:16]


def _lock_for(stream: str) -> asyncio.Lock:
    lock = _LOCKS.get(stream)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[stream] = lock
    return lock


async def _append(directory: Path, record: dict) -> None:
    """Append a single JSONL record to today's file in *directory*."""
    _ensure_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = directory / f"{today}.jsonl"
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    async with _lock_for(str(directory)):
        try:
            # Synchronous file write inside an asyncio lock — fast enough for our
            # write rate (low hundreds/sec worst case) and avoids pulling in aiofiles.
            with open(path, "ab") as f:
                f.write(line)
        except OSError as exc:
            _warn_once(str(path), f"log append failed for {path}: {exc}")


# ── Public write APIs ──────────────────────────────────────────────────

async def log_api_call(
    *,
    token_id: str | None,
    method: str,
    path: str,
    model: str | None = None,
    provider: str | None = None,
    status_code: int,
    latency_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached: bool = False,
    error: str | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> None:
    """Record a single completed gateway request."""
    rec = {
        "ts":                datetime.now(timezone.utc).isoformat(),
        "token_id":          token_id or "anon",
        "method":            method,
        "path":              path,
        "model":             model,
        "provider":          provider,
        "status_code":       status_code,
        "latency_ms":        latency_ms,
        "prompt_tokens":     prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached":            cached,
        "request_id":        request_id,
        "client_ip":         client_ip,
        "error":             _sanitise(error) if error else None,
    }
    await _append(_API_DIR, rec)


def resolve_actor(request) -> tuple[str, str]:
    """
    Return ``(actor_email, actor_role)`` from a FastAPI Request.

    Falls back to gateway-token identity, then ``"system"`` if neither is
    present. Never raises.
    """
    try:
        from app.auth.sso import get_session_user  # local import to avoid cycles
        u = get_session_user(request)
        if u and u.get("email"):
            return str(u["email"]), str(u.get("role") or "admin")
    except Exception:
        pass
    try:
        tname = getattr(request.state, "gateway_token_name", None)
        tid = getattr(request.state, "gateway_token_id", None)
        if tname or tid:
            return f"gateway:{tname or tid}", "gateway_token"
    except Exception:
        pass
    return "system", "system"


def client_ip_of(request) -> str | None:
    """Best-effort client IP, honoring X-Forwarded-For."""
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else None
    except Exception:
        return None


async def log_activity(
    *,
    actor_email: str | None,
    actor_role: str | None,
    action: str,
    target: str,
    before: Any = None,
    after: Any = None,
    request_ip: str | None = None,
    note: str | None = None,
) -> None:
    """
    Record an admin/user mutation.

    ``target`` is a stable identifier like ``provider:groq`` or
    ``gateway_token:gwtk_abc`` — keep it short and queryable.

    ``before``/``after`` are the values that changed; secret-shaped strings
    are replaced with ``_secret_fingerprint()`` so the audit log never
    contains plaintext keys.
    """
    def _fingerprint_secret_in(v):
        if isinstance(v, str) and any(
            r.search(v) for r in (_RE_AIZA, _RE_HF, _RE_NVAPI, _RE_GSK,
                                  _RE_CFUT, _RE_ARBKEY, _RE_SK)
        ):
            return _secret_fingerprint(v)
        return _sanitise(v)

    rec = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "actor_email":  actor_email or "anon",
        "actor_role":   actor_role or "unknown",
        "action":       action,
        "target":       target,
        "before":       _fingerprint_secret_in(before),
        "after":        _fingerprint_secret_in(after),
        "request_ip":   request_ip,
        "note":         _sanitise(note) if note else None,
    }
    rec["hmac"] = _hmac_tag(rec)
    await _append(_ACTIVITY_DIR, rec)


async def log_error(
    *,
    category: str,
    message: str,
    provider: str | None = None,
    model: str | None = None,
    status_code: int | None = None,
    token_id: str | None = None,
    extra: dict | None = None,
) -> None:
    """Record a structured error for trend analysis."""
    rec = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "category":    category,
        "provider":    provider,
        "model":       model,
        "status_code": status_code,
        "token_id":    token_id,
        "message":     _sanitise(message)[:500],
        "extra":       _sanitise(extra or {}),
    }
    await _append(_ERROR_DIR, rec)


# ── Read APIs (for the weekly report and audit UI) ─────────────────────

def _iter_files_in_window(directory: Path, days: int) -> Iterable[Path]:
    """Yield JSONL files from *directory* whose date is within the last *days*."""
    if not directory.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        yield path
    # Also include .gz rotated files if we ever add compression later
    for path in sorted(directory.glob("*.jsonl.gz"), reverse=True):
        try:
            file_date = datetime.strptime(path.stem.replace(".jsonl", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        yield path


async def iter_records(stream: str, days: int = 7) -> AsyncIterator[dict]:
    """
    Yield records (newest-file-first) from one of the three streams.

    ``stream`` must be one of "api", "activity", "errors".
    """
    directory = {"api": _API_DIR, "activity": _ACTIVITY_DIR, "errors": _ERROR_DIR}[stream]
    for path in _iter_files_in_window(directory, days):
        try:
            opener = gzip.open if str(path).endswith(".gz") else open
            with opener(path, "rb") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except OSError as exc:
            logger.debug("iter_records skip %s: %s", path, exc)
        # Yield control between files
        await asyncio.sleep(0)


async def summarise(days: int = 7) -> dict:
    """
    Lightweight aggregation over the last *days* of logs — used by the
    consolidated email report. Counts only; no PII.
    """
    api_total       = 0
    api_errors      = 0
    api_by_provider: dict[str, int] = {}
    api_by_token   : dict[str, int] = {}
    err_by_category: dict[str, int] = {}
    latencies      : list[int]      = []
    activity_count  = 0
    activity_by_actor: dict[str, int] = {}

    async for r in iter_records("api", days):
        api_total += 1
        sc = r.get("status_code") or 0
        if sc >= 400:
            api_errors += 1
        p = r.get("provider") or "unknown"
        api_by_provider[p] = api_by_provider.get(p, 0) + 1
        t = r.get("token_id") or "anon"
        api_by_token[t] = api_by_token.get(t, 0) + 1
        if r.get("latency_ms"):
            latencies.append(int(r["latency_ms"]))

    async for r in iter_records("errors", days):
        c = r.get("category") or "unknown"
        err_by_category[c] = err_by_category.get(c, 0) + 1

    async for r in iter_records("activity", days):
        activity_count += 1
        a = r.get("actor_email") or "anon"
        activity_by_actor[a] = activity_by_actor.get(a, 0) + 1

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    return {
        "days":                days,
        "api_total":           api_total,
        "api_errors":          api_errors,
        "api_error_rate_pct":  round(api_errors / api_total * 100, 1) if api_total else 0,
        "p50_latency_ms":      p50,
        "p95_latency_ms":      p95,
        "api_by_provider":     dict(sorted(api_by_provider.items(), key=lambda x: -x[1])[:10]),
        "api_by_token":        dict(sorted(api_by_token.items(), key=lambda x: -x[1])[:10]),
        "err_by_category":     dict(sorted(err_by_category.items(), key=lambda x: -x[1])[:10]),
        "activity_count":      activity_count,
        "activity_by_actor":   dict(sorted(activity_by_actor.items(), key=lambda x: -x[1])[:10]),
    }


# ── Retention janitor ──────────────────────────────────────────────────

def _prune_directory(directory: Path, retention_days: int) -> int:
    """Delete files older than *retention_days*. Returns count deleted."""
    if not directory.exists():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date()
    deleted = 0
    for path in directory.iterdir():
        # Accept both "YYYY-MM-DD.jsonl" and "YYYY-MM-DD.jsonl.gz"
        stem = path.name.replace(".jsonl.gz", "").replace(".jsonl", "")
        try:
            file_date = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                logger.debug("prune unlink failed for %s: %s", path, exc)
    return deleted


async def prune_now() -> dict:
    """Run the retention janitor once and return a summary."""
    summary = {
        "api":      _prune_directory(_API_DIR,      LOG_RETENTION_DAYS),
        "activity": _prune_directory(_ACTIVITY_DIR, LOG_RETENTION_DAYS),
        "errors":   _prune_directory(_ERROR_DIR,    LOG_RETENTION_DAYS),
    }
    total = sum(summary.values())
    if total:
        logger.info("Persistent log janitor pruned %d files: %s", total, summary)
    return summary


_janitor_task: asyncio.Task | None = None


async def _janitor_loop() -> None:
    """Run prune_now() once per day at ~03:00 UTC."""
    logger.info("Log janitor started (retention=%dd, root=%s)",
                LOG_RETENTION_DAYS, _LOG_ROOT)
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await prune_now()
        except asyncio.CancelledError:
            logger.info("Log janitor cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("Log janitor loop error: %s", exc, exc_info=True)
            await asyncio.sleep(3600)


def start_janitor() -> None:
    global _janitor_task
    if _janitor_task and not _janitor_task.done():
        return
    _ensure_dirs()
    _janitor_task = asyncio.create_task(_janitor_loop())


def stop_janitor() -> None:
    global _janitor_task
    if _janitor_task and not _janitor_task.done():
        _janitor_task.cancel()


def write_error(record: dict) -> None:
    """Append a frontend / UI error record to the daily errors JSONL file.

    Non-blocking — silently drops on disk-full or any I/O error so a runaway
    UI bug can't take the gateway down.
    """
    try:
        import json, time as _t, os as _os
        record = dict(record or {})
        record.setdefault("ts", _t.strftime("%Y-%m-%dT%H:%M:%S+00:00", _t.gmtime()))
        path = _os.path.join(
            _os.environ.get("ARBITER_LOG_DIR", "/app/data/logs"),
            "errors",
            _t.strftime("%Y-%m-%d.jsonl", _t.gmtime()),
        )
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        return
