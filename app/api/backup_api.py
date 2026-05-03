"""
Enterprise backup system for Arbiter — OCI Object Storage (S3-compatible).

Bucket isolation
────────────────
ALL operations are strictly scoped to the ``{BACKUP_S3_PREFIX}/`` prefix
(default: ``arbiter/backups/``).  Objects outside this prefix are never
listed, read, or deleted — making it safe to share the OCI bucket with
other applications.

Backup layout
─────────────
  arbiter/backups/
    full/
      arbiter-full-20260501T010000Z.tar.gz
    incremental/
      arbiter-incr-20260501T020000Z.tar.gz
    manifest.json              ← catalog; also cached in Redis for fast reads

Archive contents
────────────────
  Full backup:
    backup_meta.json           timestamp, version, type, redis key count
    redis_export.json          all arbiter:* Redis keys (value + TTL)
    data/arbiter_state.json    users / custom providers / model toggles / prefs

  Incremental backup:
    backup_meta.json
    redis_delta.json           config + gateway-token keys + last-48h stats
    data/arbiter_state.json    only if changed since last backup

Retention (enforced after every scheduled backup)
──────────────────────────────────────────────────
  Incremental backups  older than  7 days  → deleted automatically
  Full backups         older than 90 days  → deleted automatically

Storage quota (scoped to arbiter/backups/ only)
────────────────────────────────────────────────
  Max 10 GB.  Warning cached in Redis as ``arbiter:backup:storage_warning``
  and shown in UI when > 9 GB.

Redis keys used by this module
───────────────────────────────
  arbiter:backup:last_full_ts    epoch of last successful full backup
  arbiter:backup:last_incr_ts    epoch of last successful incremental backup
  arbiter:backup:storage_bytes   last-measured storage usage (bytes)
  arbiter:backup:storage_warning "1" when > 9 GB
  arbiter:backup:running         lock key (30-min TTL) prevents overlapping jobs
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.api.users_api import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["Backup"])

DATA_DIR = Path("/app/data")           # matches docker-compose volume
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backup")

# ── Retention limits ─────────────────────────────────────────────────────────
_INCR_RETENTION_DAYS = 7
_FULL_RETENTION_DAYS = 90

# ── Storage warning threshold ────────────────────────────────────────────────
_WARN_BYTES = int(settings.BACKUP_MAX_GB * 0.9 * 1024 ** 3)
_MAX_BYTES  = int(settings.BACKUP_MAX_GB * 1024 ** 3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s3_client():
    """Build a boto3 S3 client for OCI Object Storage (path-style)."""
    import boto3  # lazy import — installed at build time only
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.BACKUP_S3_ENDPOINT,
        aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY,
        aws_secret_access_key=settings.BACKUP_S3_SECRET_KEY,
        region_name=settings.BACKUP_S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path", "payload_signing_enabled": False},
            retries={"max_attempts": 3, "mode": "adaptive"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


async def _run(func, *args, **kwargs):
    """Run a sync function in the thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs))


def _prefix(subpath: str = "") -> str:
    """Fully-qualified S3 key under our strict prefix."""
    base = settings.BACKUP_S3_PREFIX.rstrip("/")
    if subpath:
        return f"{base}/{subpath.lstrip('/')}"
    return base


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _make_archive(files: dict[str, bytes]) -> bytes:
    """Create an in-memory .tar.gz from {relative_path: bytes} mapping."""
    buf = io.BytesIO()
    mtime = int(datetime.now(timezone.utc).timestamp())
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=path)
            info.size  = len(data)
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _read_archive(data: bytes) -> dict[str, bytes]:
    """Extract a .tar.gz and return {relative_path: bytes}."""
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                fobj = tar.extractfile(member)
                if fobj:
                    result[member.name] = fobj.read()
    return result


# ---------------------------------------------------------------------------
# Redis export / import helpers
# ---------------------------------------------------------------------------

async def _export_redis(redis, pattern: str = "arbiter:*") -> dict:
    """Dump all matching Redis keys with their values and TTLs."""
    export: dict[str, Any] = {}
    try:
        async for key in redis.scan_iter(pattern):
            try:
                value = await redis.get(key)
                ttl   = await redis.ttl(key)  # -1 = no TTL, -2 = gone
                if value is not None:
                    export[key] = {"value": value, "ttl": ttl}
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Redis export error: %s", exc)
    return export


async def _import_redis(redis, export: dict) -> int:
    """Restore keys from an export dict.  Returns count of keys restored."""
    restored = 0
    for key, meta in export.items():
        try:
            value = meta.get("value", "")
            ttl   = meta.get("ttl", -1)
            if isinstance(ttl, int) and ttl > 0:
                await redis.set(key, value, ex=ttl)
            else:
                await redis.set(key, value)
            restored += 1
        except Exception:
            continue
    return restored


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _load_manifest_sync(s3) -> dict:
    """Download and parse manifest.json from S3.  Returns {} on miss."""
    try:
        obj = s3.get_object(
            Bucket=settings.BACKUP_S3_BUCKET,
            Key=_prefix("manifest.json"),
        )
        return json.loads(obj["Body"].read())
    except Exception:
        return {"schema": 1, "backups": []}


def _save_manifest_sync(s3, manifest: dict) -> None:
    data = json.dumps(manifest, indent=2).encode()
    s3.put_object(
        Bucket=settings.BACKUP_S3_BUCKET,
        Key=_prefix("manifest.json"),
        Body=data,
        ContentLength=len(data),
        ContentType="application/json",
    )


# ---------------------------------------------------------------------------
# Core backup functions
# ---------------------------------------------------------------------------

async def run_backup(redis, backup_type: str = "incremental") -> dict:
    """Create and upload a backup.

    backup_type: "full" | "incremental"
    Returns: manifest entry dict.
    """
    if not settings.BACKUP_ENABLED:
        raise RuntimeError("Backup is disabled (BACKUP_ENABLED=false)")
    if not settings.BACKUP_S3_ENDPOINT:
        raise RuntimeError("BACKUP_S3_ENDPOINT is not configured")

    # ── Distributed lock ─────────────────────────────────────────────────────
    lock_key = "arbiter:backup:running"
    acquired = await redis.set(lock_key, "1", nx=True, ex=1800)  # 30-min TTL
    if not acquired:
        raise RuntimeError("A backup is already in progress")

    try:
        tag  = _now_tag()
        year_month = tag[:4] + "/" + tag[4:6]
        bt   = "full" if backup_type == "full" else "incremental"
        name = f"arbiter-{bt}-{tag}.tar.gz"
        obj_key = _prefix(f"{bt}/{year_month}/{name}")

        meta = {
            "key":       obj_key,
            "type":      bt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version":   "unknown",
            "label":     f"{'Weekly full' if bt == 'full' else 'Daily incremental'} backup",
        }
        try:
            from app.main import APP_VERSION
            meta["version"] = APP_VERSION
        except ImportError:
            pass

        # ── Build archive ─────────────────────────────────────────────────────
        files: dict[str, bytes] = {}

        if bt == "full":
            # Redis: export all arbiter:* keys
            redis_dump = await _export_redis(redis, "arbiter:*")
            meta["redis_key_count"] = len(redis_dump)
            files["redis_export.json"] = json.dumps(redis_dump, indent=2).encode()
        else:
            # Incremental: config + gateway tokens + last 48h stats keys
            incr_patterns = [
                "arbiter:config:*",
                "arbiter:gateway:tokens",
                "arbiter:runtime:*",
            ]
            redis_dump: dict = {}
            for pat in incr_patterns:
                partial = await _export_redis(redis, pat)
                redis_dump.update(partial)
            # Add the two most recent days of daily rollup keys
            from datetime import timedelta
            from app.observability.stats import _today
            today = _today()
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            for d in (today, yesterday):
                partial = await _export_redis(redis, f"arbiter:stats:day:{d}:*")
                redis_dump.update(partial)
            meta["redis_key_count"] = len(redis_dump)
            files["redis_delta.json"] = json.dumps(redis_dump, indent=2).encode()

        # data/ directory
        if DATA_DIR.exists():
            for fpath in DATA_DIR.iterdir():
                if fpath.is_file():
                    try:
                        files[f"data/{fpath.name}"] = fpath.read_bytes()
                    except Exception as exc:
                        logger.warning("Could not read %s: %s", fpath, exc)

        meta["file_count"] = len(files)
        files["backup_meta.json"] = json.dumps(meta, indent=2).encode()
        archive = _make_archive(files)
        meta["size_bytes"] = len(archive)

        # ── Upload to S3 ──────────────────────────────────────────────────────
        def _upload():
            s3 = _s3_client()
            s3.put_object(
                Bucket=settings.BACKUP_S3_BUCKET,
                Key=obj_key,
                Body=archive,
                ContentLength=len(archive),
                ContentType="application/gzip",
                Metadata={
                    "arbiter-backup-type":    bt,
                    "arbiter-backup-ts":      tag,
                    "arbiter-backup-version": meta.get("version", ""),
                },
            )
            # Update manifest
            manifest = _load_manifest_sync(s3)
            if "backups" not in manifest:
                manifest["backups"] = []
            manifest["backups"].append(meta)
            manifest[f"last_{bt}_ts"] = meta["timestamp"]
            manifest["schema"] = 1
            _save_manifest_sync(s3, manifest)
            return len(archive)

        await _run(_upload)

        # ── Stamp Redis ───────────────────────────────────────────────────────
        ts_key = f"arbiter:backup:last_{bt}_ts"
        await redis.set(ts_key, datetime.now(timezone.utc).isoformat())

        # ── Apply retention policy ────────────────────────────────────────────
        asyncio.create_task(_apply_retention(redis))

        logger.info(
            "Backup complete: type=%s size=%.1fKB key=%s",
            bt, len(archive) / 1024, obj_key,
        )
        return meta

    finally:
        await redis.delete(lock_key)


async def _apply_retention(redis) -> None:
    """Delete old backups per retention policy, then update storage stats."""
    try:
        from datetime import timedelta

        def _prune():
            s3 = _s3_client()
            manifest = _load_manifest_sync(s3)
            now = datetime.now(timezone.utc)
            kept, deleted = [], []
            for entry in manifest.get("backups", []):
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    kept.append(entry)
                    continue
                age_days = (now - ts).days
                if entry.get("type") == "incremental" and age_days > _INCR_RETENTION_DAYS:
                    deleted.append(entry)
                elif entry.get("type") == "full" and age_days > _FULL_RETENTION_DAYS:
                    deleted.append(entry)
                else:
                    kept.append(entry)

            for entry in deleted:
                try:
                    s3.delete_object(
                        Bucket=settings.BACKUP_S3_BUCKET,
                        Key=entry["key"],
                    )
                    logger.info("Retention: deleted %s", entry["key"])
                except Exception as exc:
                    logger.warning("Retention delete error: %s", exc)

            manifest["backups"] = kept
            _save_manifest_sync(s3, manifest)

            # Storage accounting (prefix-scoped only)
            total_bytes = 0
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=settings.BACKUP_S3_BUCKET,
                Prefix=_prefix() + "/",
            ):
                for obj in page.get("Contents", []):
                    total_bytes += obj.get("Size", 0)
            return total_bytes

        total_bytes = await _run(_prune)
        await redis.set("arbiter:backup:storage_bytes", str(total_bytes))
        if total_bytes > _WARN_BYTES:
            await redis.set("arbiter:backup:storage_warning", "1")
        else:
            await redis.delete("arbiter:backup:storage_warning")
    except Exception as exc:
        logger.warning("Retention / storage-accounting error: %s", exc)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@router.get("/status", summary="Backup system status")
async def backup_status(request: Request) -> JSONResponse:
    redis = request.app.state.redis

    async def rget(key: str) -> Optional[str]:
        try:
            return await redis.get(key)
        except Exception:
            return None

    last_full = await rget("arbiter:backup:last_full_ts")
    last_incr = await rget("arbiter:backup:last_incr_ts")
    storage_bytes = int((await rget("arbiter:backup:storage_bytes")) or 0)
    warning = bool(await rget("arbiter:backup:storage_warning"))
    running = bool(await rget("arbiter:backup:running"))

    max_bytes = _MAX_BYTES
    return JSONResponse({
        "enabled":        settings.BACKUP_ENABLED,
        "configured":     bool(settings.BACKUP_S3_ENDPOINT),
        "running":        running,
        "last_full":      last_full,
        "last_incr":      last_incr,
        "storage_bytes":  storage_bytes,
        "storage_max_bytes": max_bytes,
        "storage_pct":    round(storage_bytes / max_bytes * 100, 1) if max_bytes > 0 else 0,
        "storage_warning": warning,
        "prefix":         settings.BACKUP_S3_PREFIX,
        "bucket":         settings.BACKUP_S3_BUCKET,
    })


@router.get("/list", summary="List all available backups")
async def backup_list(request: Request) -> JSONResponse:
    if not settings.BACKUP_S3_ENDPOINT:
        return JSONResponse({"backups": [], "error": "S3 not configured"})

    def _list():
        s3 = _s3_client()
        manifest = _load_manifest_sync(s3)
        return manifest.get("backups", [])

    try:
        backups = await _run(_list)
        # Sort newest first
        backups_sorted = sorted(
            backups,
            key=lambda b: b.get("timestamp", ""),
            reverse=True,
        )
        return JSONResponse({"backups": backups_sorted})
    except Exception as exc:
        logger.error("backup_list error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/run", summary="Trigger a manual backup", dependencies=[Depends(require_admin)])
async def backup_run(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    redis = request.app.state.redis

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    backup_type = body.get("type", "incremental")
    if backup_type not in ("full", "incremental"):
        raise HTTPException(status_code=400, detail="type must be 'full' or 'incremental'")

    if not settings.BACKUP_S3_ENDPOINT:
        raise HTTPException(status_code=503, detail="S3 backup is not configured")

    # Run as background task so the HTTP response returns immediately
    async def _do():
        try:
            await run_backup(redis, backup_type=backup_type)
        except Exception as exc:
            logger.error("Manual backup failed: %s", exc)

    background_tasks.add_task(_do)
    return JSONResponse({"status": "started", "type": backup_type})


@router.get("/storage", summary="Storage usage summary")
async def backup_storage(request: Request) -> JSONResponse:
    redis = request.app.state.redis
    if not settings.BACKUP_S3_ENDPOINT:
        return JSONResponse({"error": "S3 not configured", "total_bytes": 0})

    def _calc():
        s3 = _s3_client()
        total = 0
        full_bytes = incr_bytes = 0
        full_count = incr_count = 0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=settings.BACKUP_S3_BUCKET,
            Prefix=_prefix() + "/",
        ):
            for obj in page.get("Contents", []):
                sz = obj.get("Size", 0)
                total += sz
                key = obj.get("Key", "")
                if "/full/" in key:
                    full_bytes += sz
                    full_count += 1
                elif "/incremental/" in key:
                    incr_bytes += sz
                    incr_count += 1
        return {
            "total_bytes":      total,
            "full_bytes":       full_bytes,
            "full_count":       full_count,
            "incremental_bytes": incr_bytes,
            "incremental_count": incr_count,
            "max_bytes":        _MAX_BYTES,
            "pct_used":         round(total / _MAX_BYTES * 100, 1) if _MAX_BYTES else 0,
        }

    try:
        data = await _run(_calc)
        # Cache to Redis
        await request.app.state.redis.set(
            "arbiter:backup:storage_bytes", str(data["total_bytes"])
        )
        return JSONResponse(data)
    except Exception as exc:
        logger.error("backup_storage error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{encoded_key:path}/download", summary="Download a backup archive", dependencies=[Depends(require_admin)])
async def backup_download(encoded_key: str, request: Request) -> StreamingResponse:
    """Stream a backup .tar.gz to the client.

    ``encoded_key`` is the full S3 object key (URL-encoded by the browser).
    We validate it starts with our strict prefix before fetching.
    """
    # Security: ensure the requested key is inside our prefix
    strict_prefix = settings.BACKUP_S3_PREFIX.rstrip("/") + "/"
    if not encoded_key.startswith(strict_prefix):
        raise HTTPException(status_code=400, detail="Invalid backup key")

    if not settings.BACKUP_S3_ENDPOINT:
        raise HTTPException(status_code=503, detail="S3 not configured")

    def _fetch():
        s3 = _s3_client()
        obj = s3.get_object(
            Bucket=settings.BACKUP_S3_BUCKET,
            Key=encoded_key,
        )
        return obj["Body"].read(), obj.get("ContentLength", 0)

    try:
        data, _ = await _run(_fetch)
        filename = encoded_key.split("/")[-1]
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        logger.error("backup_download error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{encoded_key:path}/restore", summary="Restore from a backup", dependencies=[Depends(require_admin)])
async def backup_restore(encoded_key: str, request: Request) -> JSONResponse:
    """Download a backup and restore Redis keys + data/ files."""
    strict_prefix = settings.BACKUP_S3_PREFIX.rstrip("/") + "/"
    if not encoded_key.startswith(strict_prefix):
        raise HTTPException(status_code=400, detail="Invalid backup key")

    if not settings.BACKUP_S3_ENDPOINT:
        raise HTTPException(status_code=503, detail="S3 not configured")

    redis = request.app.state.redis

    def _fetch():
        s3 = _s3_client()
        obj = s3.get_object(Bucket=settings.BACKUP_S3_BUCKET, Key=encoded_key)
        return obj["Body"].read()

    try:
        archive_bytes = await _run(_fetch)
        files = _read_archive(archive_bytes)

        restored_redis = 0
        restored_files = 0

        # Restore Redis keys
        for fname in ("redis_export.json", "redis_delta.json"):
            if fname in files:
                export = json.loads(files[fname].decode())
                restored_redis = await _import_redis(redis, export)
                break

        # Restore data/ files
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            if path.startswith("data/") and path != "data/":
                dest = DATA_DIR / path[len("data/"):]
                dest.write_bytes(content)
                restored_files += 1

        logger.info(
            "Restore complete from %s: redis_keys=%d files=%d",
            encoded_key, restored_redis, restored_files,
        )
        return JSONResponse({
            "status":         "ok",
            "redis_restored": restored_redis,
            "files_restored": restored_files,
        })
    except Exception as exc:
        logger.error("backup_restore error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/{encoded_key:path}", summary="Delete a specific backup", dependencies=[Depends(require_admin)])
async def backup_delete(encoded_key: str, request: Request) -> JSONResponse:
    """Delete a single backup object from S3 and remove it from the manifest."""
    strict_prefix = settings.BACKUP_S3_PREFIX.rstrip("/") + "/"
    if not encoded_key.startswith(strict_prefix):
        raise HTTPException(status_code=400, detail="Invalid backup key")

    if not settings.BACKUP_S3_ENDPOINT:
        raise HTTPException(status_code=503, detail="S3 not configured")

    def _delete():
        s3 = _s3_client()
        s3.delete_object(Bucket=settings.BACKUP_S3_BUCKET, Key=encoded_key)
        manifest = _load_manifest_sync(s3)
        manifest["backups"] = [
            b for b in manifest.get("backups", []) if b.get("key") != encoded_key
        ]
        _save_manifest_sync(s3, manifest)

    try:
        await _run(_delete)
        return JSONResponse({"status": "deleted", "key": encoded_key})
    except Exception as exc:
        logger.error("backup_delete error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/purge-duplicates", summary="Remove duplicate backups, keep only newest per day/type", dependencies=[Depends(require_admin)])
async def backup_purge_duplicates(request: Request) -> JSONResponse:
    """Deduplicate backups: for each (date, type) group, keep only the latest
    entry and delete all others from S3 + manifest.

    This fixes the historical bug where the scheduler created hundreds of
    identical backups in a single day.
    """
    if not settings.BACKUP_S3_ENDPOINT:
        raise HTTPException(status_code=503, detail="S3 not configured")

    def _purge():
        s3 = _s3_client()
        manifest = _load_manifest_sync(s3)
        backups = manifest.get("backups", [])

        # Group by (date, type) — keep the latest in each group
        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        for entry in backups:
            ts_str = entry.get("timestamp", "")
            btype = entry.get("type", "full")
            date_key = ts_str[:10] if len(ts_str) >= 10 else "unknown"
            groups[f"{date_key}:{btype}"].append(entry)

        kept = []
        deleted_count = 0
        for group_key, entries in groups.items():
            # Sort by timestamp descending, keep first (newest)
            entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            kept.append(entries[0])
            for dup in entries[1:]:
                # Delete from S3
                try:
                    s3.delete_object(
                        Bucket=settings.BACKUP_S3_BUCKET,
                        Key=dup["key"],
                    )
                except Exception as exc:
                    logger.warning("Purge: failed to delete %s: %s", dup.get("key"), exc)
                deleted_count += 1

        manifest["backups"] = sorted(
            kept, key=lambda e: e.get("timestamp", ""), reverse=True
        )
        _save_manifest_sync(s3, manifest)
        return {"deleted": deleted_count, "remaining": len(kept)}

    try:
        result = await _run(_purge)
        logger.info(
            "Purge duplicates: deleted %d, remaining %d",
            result["deleted"], result["remaining"],
        )
        return JSONResponse({"status": "ok", **result})
    except Exception as exc:
        logger.error("purge_duplicates error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Backup page route ────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def backup_page():
    from fastapi.responses import FileResponse
    path = Path(__file__).parent.parent.parent / "static" / "backup.html"
    if not path.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>Backup page not found</h1>", status_code=404)
    return FileResponse(str(path))
