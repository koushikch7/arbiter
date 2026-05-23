"""
Weekly Model Health Check (v1.17.0).

Pings every configured provider's models with a trivial ``Hi`` completion
once a week (default: Monday 22:30 IST / 17:00 UTC) and records pass/fail
results into Redis so the daily email report can surface stale or broken
models that need to be removed from the catalogue.

Storage layout
--------------
arbiter:health:model:{provider}:{model_id}
    JSON {"status": "ok"|"fail", "last_checked": iso-ts, "error": "...",
          "latency_ms": int}
arbiter:health:model:last_run
    ISO timestamp of the most recent completed run.

The TTL on each entry is 14 days so a stale provider drops out of the
report once it's been ignored for two weekly cycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.models.schemas import ChatCompletionRequest, Message

logger = logging.getLogger(__name__)

_HEALTH_TTL = 14 * 86_400          # 14 days
_PROBE_TIMEOUT = 15.0              # per-model timeout (seconds)
_BETWEEN_MODELS_DELAY = 0.5        # gentle pacing to avoid RPM hits
_PROBE_CONCURRENCY = 4             # max parallel probes (audit fix #2)

# Track the background task so we can cancel on shutdown
_health_task: Optional[asyncio.Task] = None


def _build_probe_request(model: str) -> ChatCompletionRequest:
    """Smallest possible chat completion — 1 token reply, no streaming."""
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content="Hi")],
        max_tokens=1,
        temperature=0.0,
        stream=False,
    )


async def _probe_one(provider, model: str, redis) -> dict:
    """
    Send a minimal request to ``provider`` for ``model`` and persist the
    result. Returns the stored record (already written to Redis).
    """
    from app.providers.base import RateLimitError, ProviderError

    # Pick a key for this provider via its key pool
    api_key = None
    try:
        if hasattr(provider, "key_pool") and provider.key_pool:
            api_key = await provider.key_pool.get_best_key()
    except Exception:
        api_key = None

    if api_key is None:
        # Some providers (Pollinations, Ollama) accept anonymous calls
        api_key = ""

    req = _build_probe_request(model)
    started = time.monotonic()
    status, error_msg = "ok", ""
    rate_limited = False

    try:
        await asyncio.wait_for(
            provider.complete(req, api_key),
            timeout=_PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        status, error_msg = "fail", f"timeout > {_PROBE_TIMEOUT}s"
    except RateLimitError as exc:
        # Rate-limited != broken; flag it separately so the daily report
        # can alert when a provider is consistently saturated even though
        # the health status itself remains "ok".
        status, error_msg = "ok", f"rate-limited: {exc}"[:200]
        rate_limited = True
    except ProviderError as exc:
        status, error_msg = "fail", str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 — health probe must be defensive
        status, error_msg = "fail", f"{type(exc).__name__}: {exc}"[:200]

    record = {
        "status": status,
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "error": error_msg,
        "rate_limited": rate_limited,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }

    try:
        key = f"arbiter:health:model:{provider.name}:{model}"
        await redis.set(key, json.dumps(record), ex=_HEALTH_TTL)
    except Exception as exc:
        logger.warning("Failed to persist health record for %s/%s: %s",
                       provider.name, model, exc)

    return record


async def run_weekly_health_check(app) -> dict:
    """
    Probe every model across every provider currently registered on the app.
    Returns a summary dict with totals. Safe to call manually for testing.
    """
    redis = app.state.redis
    providers = getattr(app.state, "providers", {}) or {}

    started = time.monotonic()
    summary = {"providers": 0, "models": 0, "ok": 0, "fail": 0, "rate_limited": 0,
               "rate_limited_by_provider": {}}
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _bounded_probe(provider, model):
        async with semaphore:
            rec = await _probe_one(provider, model, redis)
            await asyncio.sleep(_BETWEEN_MODELS_DELAY)
            return provider.name, rec

    # Build the full probe set across providers (model_health probes can run
    # in parallel across providers — they target different upstreams + keys)
    tasks = []
    for pname, provider in providers.items():
        try:
            models = list(getattr(provider, "models", []) or [])
        except Exception:
            models = []
        if not models:
            continue
        summary["providers"] += 1
        for m in models:
            summary["models"] += 1
            tasks.append(_bounded_probe(provider, m))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item in results:
        if isinstance(item, Exception):
            summary["fail"] += 1
            continue
        try:
            pname, rec = item
        except Exception:
            summary["fail"] += 1
            continue
        if not isinstance(rec, dict):
            summary["fail"] += 1
            continue
        if rec.get("status") == "ok":
            summary["ok"] += 1
        else:
            summary["fail"] += 1
        if rec.get("rate_limited"):
            summary["rate_limited"] += 1
            summary["rate_limited_by_provider"][pname] = (
                summary["rate_limited_by_provider"].get(pname, 0) + 1
            )

    summary["elapsed_s"] = round(time.monotonic() - started, 1)

    try:
        await redis.set(
            "arbiter:health:model:last_run",
            datetime.now(timezone.utc).isoformat(),
            ex=_HEALTH_TTL,
        )
        await redis.set(
            "arbiter:health:model:last_summary",
            json.dumps(summary),
            ex=_HEALTH_TTL,
        )
    except Exception:
        pass

    logger.info(
        "Weekly model health check complete: %d ok / %d fail across %d providers in %.1fs",
        summary["ok"], summary["fail"], summary["providers"], summary["elapsed_s"],
    )
    return summary


async def _scheduler_loop(app):
    """
    Wait until the next Monday 17:00 UTC (= 22:30 IST) and run the health
    check. After it completes, sleep until next Monday.
    """
    logger.info("Weekly model health scheduler started (Mondays 17:00 UTC / 22:30 IST)")
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Monday = weekday() 0
            days_until_monday = (0 - now.weekday()) % 7
            target = (now + timedelta(days=days_until_monday)).replace(
                hour=17, minute=0, second=0, microsecond=0,
            )
            if target <= now:
                target += timedelta(days=7)
            wait_s = (target - now).total_seconds()
            logger.debug("Next weekly health check at %s (in %.0fh)",
                         target.isoformat(), wait_s / 3600)
            await asyncio.sleep(wait_s)
            await run_weekly_health_check(app)
        except asyncio.CancelledError:
            logger.info("Weekly model health scheduler cancelled")
            break
        except Exception as exc:
            logger.error("Weekly health scheduler error: %s", exc, exc_info=True)
            await asyncio.sleep(3600)


def start_scheduler(app) -> None:
    global _health_task
    if _health_task and not _health_task.done():
        return
    _health_task = asyncio.create_task(_scheduler_loop(app))
    logger.info("Weekly model health scheduler task created")


def stop_scheduler() -> None:
    global _health_task
    if _health_task and not _health_task.done():
        _health_task.cancel()
        logger.info("Weekly model health scheduler stopped")
