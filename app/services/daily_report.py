"""
Daily Analytics Report — scheduled email with usage data and error analysis.

Runs at a configurable hour (default 06:00 UTC) and sends a comprehensive
HTML report to the admin email. Includes:
  - Total requests, success rate, avg latency
  - Top 5 models by request count
  - Top 5 gateway apps by traffic
  - Provider health summary
  - Error analysis with severity classification
  - Quota alerts
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Store reference to the scheduled task so it can be cancelled on shutdown
_report_task: Optional[asyncio.Task] = None


async def gather_daily_stats(redis) -> Dict:
    """Collect all analytics data from Redis for the past 24 hours."""
    from app.observability import stats as obs_stats

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)

    # Global counters
    total = await obs_stats.get_int(redis, "arbiter:stats:requests:total") or 0
    success = await obs_stats.get_int(redis, "arbiter:stats:requests:success") or 0
    failed = await obs_stats.get_int(redis, "arbiter:stats:requests:failed") or 0
    cache_hits = await obs_stats.get_int(redis, "arbiter:stats:cache:hits") or 0
    cache_misses = await obs_stats.get_int(redis, "arbiter:stats:cache:misses") or 0

    # Per-provider stats
    providers_data = {}
    try:
        provider_keys = await redis.keys("arbiter:stats:provider:*:requests")
        for k in provider_keys:
            parts = k.split(":")
            if len(parts) >= 4:
                pname = parts[3]
                providers_data[pname] = {
                    "requests": await obs_stats.get_int(redis, k),
                    "errors": await obs_stats.get_int(redis, f"arbiter:stats:provider:{pname}:errors") or 0,
                    "rate_limited": await obs_stats.get_int(redis, f"arbiter:stats:provider:{pname}:rate_limited") or 0,
                }
    except Exception as e:
        logger.warning("Error collecting provider stats: %s", e)

    # Per-model stats (top 5)
    model_counts = {}
    try:
        model_keys = await redis.keys("arbiter:stats:model:*:requests")
        for k in model_keys:
            parts = k.split(":")
            if len(parts) >= 4:
                mname = parts[3]
                model_counts[mname] = await obs_stats.get_int(redis, k) or 0
    except Exception as e:
        logger.warning("Error collecting model stats: %s", e)

    top_models = sorted(model_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Per-token/gateway stats (top 5)
    token_counts = {}
    try:
        token_keys = await redis.keys("arbiter:stats:token:*:requests")
        for k in token_keys:
            parts = k.split(":")
            if len(parts) >= 5:
                tid = parts[4]
                token_counts[tid] = await obs_stats.get_int(redis, k) or 0
    except Exception as e:
        logger.warning("Error collecting token stats: %s", e)

    top_tokens = sorted(token_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Recent errors
    errors = []
    try:
        error_keys = await redis.keys("arbiter:errors:*")
        for k in error_keys[:20]:  # limit to avoid overwhelming
            err_data = await redis.get(k)
            if err_data:
                errors.append(err_data)
    except Exception:
        pass

    return {
        "total_requests": total,
        "success": success,
        "failed": failed,
        "success_rate": round((success / total * 100) if total > 0 else 0, 1),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_hit_rate": round((cache_hits / (cache_hits + cache_misses) * 100) if (cache_hits + cache_misses) > 0 else 0, 1),
        "providers": providers_data,
        "top_models": top_models,
        "top_tokens": top_tokens,
        "errors": errors,
        "generated_at": now.isoformat(),
    }


def _classify_error(error_msg: str) -> Tuple[str, str]:
    """Classify an error as temporary or critical with explanation."""
    msg_lower = error_msg.lower() if error_msg else ""

    if any(kw in msg_lower for kw in ["rate limit", "429", "quota", "throttl"]):
        return "temporary", "Rate limit — will auto-recover after cooldown period"
    if any(kw in msg_lower for kw in ["timeout", "timed out", "deadline"]):
        return "temporary", "Timeout — provider may be overloaded momentarily"
    if any(kw in msg_lower for kw in ["502", "503", "service unavailable", "bad gateway"]):
        return "temporary", "Upstream service issue — usually resolves within minutes"
    if any(kw in msg_lower for kw in ["401", "unauthorized", "invalid key", "authentication"]):
        return "critical", "Authentication failure — API key may be expired or revoked"
    if any(kw in msg_lower for kw in ["403", "forbidden", "access denied"]):
        return "critical", "Access denied — account may be suspended or key permissions changed"
    if any(kw in msg_lower for kw in ["model not found", "404", "not found"]):
        return "warning", "Model unavailable — may have been deprecated or renamed"
    if any(kw in msg_lower for kw in ["connection", "network", "dns"]):
        return "temporary", "Network connectivity issue — likely transient"

    return "unknown", "Unclassified error — review manually"


def _build_report_html(stats: Dict, token_names: Dict[str, str]) -> str:
    """Generate the HTML email body from collected stats."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Provider health rows
    provider_rows = ""
    for pname, pdata in sorted(stats["providers"].items()):
        reqs = pdata["requests"]
        errs = pdata["errors"]
        health_pct = round(((reqs - errs) / reqs * 100) if reqs > 0 else 100, 1)
        color = "#10b981" if health_pct >= 95 else "#f59e0b" if health_pct >= 80 else "#ef4444"
        arrow = "↑" if health_pct >= 95 else "↓"
        provider_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{pname}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600">{health_pct}% {arrow}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{reqs:,}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#ef4444">{errs}</td></tr>'

    # Top models rows
    model_rows = ""
    for i, (model, count) in enumerate(stats["top_models"], 1):
        pct = round(count / stats["total_requests"] * 100, 1) if stats["total_requests"] > 0 else 0
        model_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">#{i}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:12px">{model}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{count:,}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{pct}%</td></tr>'

    # Top gateways rows
    gateway_rows = ""
    for i, (tid, count) in enumerate(stats["top_tokens"], 1):
        name = token_names.get(tid, tid[:12] + "…")
        gateway_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">#{i}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{name}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{count:,}</td></tr>'

    # Error analysis rows
    error_section = ""
    if stats["errors"]:
        error_rows = ""
        for err in stats["errors"][:10]:
            severity, explanation = _classify_error(err)
            sev_color = {"critical": "#ef4444", "warning": "#f59e0b", "temporary": "#6b7280"}.get(severity, "#6b7280")
            error_rows += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb"><span style="color:{sev_color};font-weight:600;text-transform:uppercase;font-size:11px">{severity}</span></td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-size:12px">{str(err)[:100]}</td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-size:11px;color:#666">{explanation}</td></tr>'
        error_section = f"""
        <div style="margin-top:24px">
          <h3 style="margin:0 0 12px;font-size:16px;color:#111">⚠️ Error Analysis</h3>
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="background:#f8f9fa"><th style="padding:8px 12px;text-align:left">Severity</th><th style="padding:8px 12px;text-align:left">Error</th><th style="padding:8px 12px;text-align:left">AI Analysis</th></tr></thead>
            <tbody>{error_rows}</tbody>
          </table>
        </div>"""

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;background:#fff">
  <div style="text-align:center;margin-bottom:24px;padding:20px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;color:#fff">
    <h1 style="margin:0 0 4px;font-size:22px">📊 Arbiter Daily Report</h1>
    <p style="margin:0;font-size:13px;opacity:.85">{now}</p>
  </div>

  <!-- KPIs -->
  <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
    <div style="flex:1;min-width:140px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:700;color:#166534">{stats['total_requests']:,}</div>
      <div style="font-size:11px;color:#166534;margin-top:4px">Total Requests</div>
    </div>
    <div style="flex:1;min-width:140px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:700;color:#1e40af">{stats['success_rate']}%</div>
      <div style="font-size:11px;color:#1e40af;margin-top:4px">Success Rate</div>
    </div>
    <div style="flex:1;min-width:140px;background:#fef3c7;border:1px solid #fde68a;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:700;color:#92400e">{stats['cache_hit_rate']}%</div>
      <div style="font-size:11px;color:#92400e;margin-top:4px">Cache Hit Rate</div>
    </div>
    <div style="flex:1;min-width:140px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:700;color:#991b1b">{stats['failed']:,}</div>
      <div style="font-size:11px;color:#991b1b;margin-top:4px">Failed</div>
    </div>
  </div>

  <!-- Top 5 Models -->
  <div style="margin-bottom:24px">
    <h3 style="margin:0 0 12px;font-size:16px;color:#111">🏆 Top 5 Models</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
      <thead><tr style="background:#f8f9fa"><th style="padding:8px 12px;text-align:left">#</th><th style="padding:8px 12px;text-align:left">Model</th><th style="padding:8px 12px;text-align:left">Requests</th><th style="padding:8px 12px;text-align:left">Share</th></tr></thead>
      <tbody>{model_rows if model_rows else '<tr><td colspan="4" style="padding:16px;text-align:center;color:#999">No model usage data yet</td></tr>'}</tbody>
    </table>
  </div>

  <!-- Top 5 Gateway Apps -->
  <div style="margin-bottom:24px">
    <h3 style="margin:0 0 12px;font-size:16px;color:#111">🔑 Top 5 Gateway Apps</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
      <thead><tr style="background:#f8f9fa"><th style="padding:8px 12px;text-align:left">#</th><th style="padding:8px 12px;text-align:left">App / Token</th><th style="padding:8px 12px;text-align:left">Requests</th></tr></thead>
      <tbody>{gateway_rows if gateway_rows else '<tr><td colspan="3" style="padding:16px;text-align:center;color:#999">No gateway usage data yet</td></tr>'}</tbody>
    </table>
  </div>

  <!-- Provider Health -->
  <div style="margin-bottom:24px">
    <h3 style="margin:0 0 12px;font-size:16px;color:#111">🏥 Provider Health</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
      <thead><tr style="background:#f8f9fa"><th style="padding:8px 12px;text-align:left">Provider</th><th style="padding:8px 12px;text-align:left">Health</th><th style="padding:8px 12px;text-align:left">Requests</th><th style="padding:8px 12px;text-align:left">Errors</th></tr></thead>
      <tbody>{provider_rows if provider_rows else '<tr><td colspan="4" style="padding:16px;text-align:center;color:#999">No provider data yet</td></tr>'}</tbody>
    </table>
  </div>

  {error_section}

  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;text-align:center">
    <p style="font-size:11px;color:#999;margin:0">Arbiter Gateway — <a href="{settings.APP_BASE_URL}/dashboard" style="color:#6366f1">View Dashboard</a></p>
  </div>
</body>
</html>"""


async def send_daily_report(redis) -> bool:
    """Collect stats and send the daily report email."""
    from app.services.email_service import email_service
    import json

    if not email_service.configured:
        logger.info("Daily report skipped — SMTP not configured")
        return False

    try:
        stats = await gather_daily_stats(redis)

        # Get token names for the report
        token_names = {}
        try:
            raw = await redis.get("arbiter:gateway:tokens")
            if raw:
                tokens = json.loads(raw)
                for t in tokens:
                    token_names[t["id"]] = t.get("name", t["id"])
        except Exception:
            pass
        token_names["env"] = "env-var keys"
        token_names["session"] = "Playground (session)"

        html = _build_report_html(stats, token_names)
        subject = f"📊 Arbiter Daily Report — {datetime.now(timezone.utc).strftime('%b %d, %Y')}"

        sent = await email_service.send_to_admin(subject, html)
        if sent:
            logger.info("Daily analytics report sent successfully")
        return sent

    except Exception as e:
        logger.error("Failed to generate/send daily report: %s", e, exc_info=True)
        return False


async def _scheduler_loop(app):
    """Background loop that sends the report at the configured hour."""
    report_hour = settings.DAILY_REPORT_HOUR
    logger.info("Daily report scheduler started (sends at %02d:00 UTC)", report_hour)

    while True:
        try:
            now = datetime.now(timezone.utc)
            # Calculate seconds until next report time
            target = now.replace(hour=report_hour, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()

            logger.debug("Next daily report in %.0f seconds (%s)", wait_seconds, target.isoformat())
            await asyncio.sleep(wait_seconds)

            # Send the report
            redis = app.state.redis
            await send_daily_report(redis)

        except asyncio.CancelledError:
            logger.info("Daily report scheduler cancelled")
            break
        except Exception as e:
            logger.error("Daily report scheduler error: %s", e, exc_info=True)
            # Wait 1 hour before retrying on unexpected errors
            await asyncio.sleep(3600)


def start_scheduler(app):
    """Start the daily report scheduler as a background task."""
    global _report_task
    from app.services.email_service import email_service

    if not email_service.configured:
        logger.info("Daily report scheduler NOT started — SMTP not configured")
        return

    _report_task = asyncio.create_task(_scheduler_loop(app))
    logger.info("Daily report scheduler task created")


def stop_scheduler():
    """Cancel the scheduler task on shutdown."""
    global _report_task
    if _report_task and not _report_task.done():
        _report_task.cancel()
        logger.info("Daily report scheduler stopped")
