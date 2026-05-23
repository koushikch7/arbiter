"""
Daily Analytics Report — scheduled email with usage data and error analysis.

Runs at 22:00 IST (16:30 UTC) daily and sends a comprehensive HTML report
to the admin email. Includes:
  - Last 24 Hours section (daily rollup counters)
  - All-Time / Lifetime section (global counters)
  - Top 5 models by request count
  - Top 5 gateway apps by traffic
  - Provider health summary
  - Error analysis with severity classification

All timestamps displayed in IST (UTC+5:30).
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re as _re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# IST = UTC + 5:30
_IST = timezone(timedelta(hours=5, minutes=30))

# Store reference to the scheduled task so it can be cancelled on shutdown
_report_task: Optional[asyncio.Task] = None


# ── Secret-shaped patterns we never want to leak in an outbound email ──
_SECRET_PATTERNS = (
    _re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),           # OpenAI / OpenRouter
    _re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"),           # Google / Gemini
    _re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),               # HuggingFace
    _re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}\b"),         # NVIDIA
    _re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}\b"),           # Groq
    _re.compile(r"\bcfut_[A-Za-z0-9_\-]{20,}\b"),          # Cloudflare token
    _re.compile(r"\barbiter-sk-[A-Za-z0-9_\-]{32,}\b"),    # our own gateway tokens
    _re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{16,}"),  # generic auth headers
    _re.compile(r"\b[A-Fa-f0-9]{40,}\b"),                   # long hex tokens
)


def _sanitise_for_email(text: str | None, *, max_len: int = 200) -> str:
    """
    Strip secret-shaped strings out of error messages before they are
    embedded in the outbound HTML email, then truncate and HTML-escape.

    The renderer used to dump raw error text directly into the message
    body, which could leak keys or signed URLs if a provider echoed them
    back in an error response. We now replace anything matching the
    secret-shape regexes with ``<redacted>`` before display.
    """
    if not text:
        return ""
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("<redacted>", s)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return _html.escape(s)


async def gather_daily_stats(redis) -> Dict:
    """Collect all analytics data — both last-24h (daily rollups) and lifetime."""
    from app.observability import stats as obs_stats

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Lifetime global counters ───────────────────────────────────────────
    total = await obs_stats.get_int(redis, "arbiter:stats:requests_total") or 0
    success = await obs_stats.get_int(redis, "arbiter:stats:requests_success") or 0
    failed = await obs_stats.get_int(redis, "arbiter:stats:requests_failed") or 0
    cache_hits = await obs_stats.get_int(redis, "arbiter:stats:cache_hits") or 0
    cache_misses = await obs_stats.get_int(redis, "arbiter:stats:cache_misses") or 0

    # ── Last 24h from daily rollup keys (today + yesterday) ────────────────
    day_reqs_today = await obs_stats.get_int(redis, f"arbiter:stats:day:{today}:requests") or 0
    day_succ_today = await obs_stats.get_int(redis, f"arbiter:stats:day:{today}:success") or 0
    day_errs_today = await obs_stats.get_int(redis, f"arbiter:stats:day:{today}:errors") or 0
    day_reqs_yest = await obs_stats.get_int(redis, f"arbiter:stats:day:{yesterday}:requests") or 0
    day_succ_yest = await obs_stats.get_int(redis, f"arbiter:stats:day:{yesterday}:success") or 0
    day_errs_yest = await obs_stats.get_int(redis, f"arbiter:stats:day:{yesterday}:errors") or 0

    # Approximate last 24h: today's full day + yesterday's partial.
    # Since report runs at 22:00 IST (16:30 UTC), most of "today" is captured.
    # We sum both days for a conservative 24h window.
    day24_requests = day_reqs_today + day_reqs_yest
    day24_success = day_succ_today + day_succ_yest
    day24_failed = day_errs_today + day_errs_yest

    # 24h per-model (today only — most relevant)
    day_model_counts = {}
    try:
        day_model_keys = await redis.keys(f"arbiter:stats:day:{today}:model:*:requests")
        for k in day_model_keys:
            # key = "arbiter:stats:day:2026-05-04:model:{name}:requests"
            parts = k.split(":")
            if len(parts) >= 7:
                mname = parts[5]
                day_model_counts[mname] = await obs_stats.get_int(redis, k) or 0
    except Exception:
        pass
    day_top_models = sorted(day_model_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # 24h per-provider (today only)
    day_provider_counts = {}
    try:
        day_prov_keys = await redis.keys(f"arbiter:stats:day:{today}:provider:*:requests")
        for k in day_prov_keys:
            # key = "arbiter:stats:day:2026-05-04:provider:{name}:requests"
            parts = k.split(":")
            if len(parts) >= 7:
                pname = parts[5]
                day_provider_counts[pname] = await obs_stats.get_int(redis, k) or 0
    except Exception:
        pass

    # ── Lifetime per-provider stats ────────────────────────────────────────
    providers_data = {}
    try:
        provider_keys = await redis.keys("arbiter:stats:provider:*:success")
        for k in provider_keys:
            parts = k.split(":")
            if len(parts) >= 5:
                pname = parts[3]
                reqs = await obs_stats.get_int(redis, f"arbiter:stats:provider:{pname}:success") or 0
                errs = await obs_stats.get_int(redis, f"arbiter:stats:provider:{pname}:errors") or 0
                rl = await obs_stats.get_int(redis, f"arbiter:stats:provider:{pname}:rate_limited") or 0
                providers_data[pname] = {
                    "requests": reqs + errs + rl,
                    "errors": errs + rl,
                    "rate_limited": rl,
                }
    except Exception as e:
        logger.warning("Error collecting provider stats: %s", e)

    # ── Lifetime per-model (top 5) ────────────────────────────────────────
    model_counts = {}
    try:
        model_keys = await redis.keys("arbiter:stats:model:*:requests")
        for k in model_keys:
            parts = k.split(":")
            if len(parts) >= 5:
                mname = parts[3]
                model_counts[mname] = await obs_stats.get_int(redis, k) or 0
    except Exception as e:
        logger.warning("Error collecting model stats: %s", e)

    top_models = sorted(model_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── Lifetime per-token (top 5) ────────────────────────────────────────
    token_counts = {}
    try:
        token_keys = await redis.keys("arbiter:stats:token:*:requests")
        for k in token_keys:
            parts = k.split(":")
            if len(parts) == 5:
                tid = parts[3]
                token_counts[tid] = await obs_stats.get_int(redis, k) or 0
    except Exception as e:
        logger.warning("Error collecting token stats: %s", e)

    top_tokens = sorted(token_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── High error-rate alerts (refined in v1.18.0) ──────────────────────
    # Require a meaningful sample size (100 requests) and only count
    # non-rate-limited failures so an upstream 429 burst does not look like
    # an outage. Threshold itself is unchanged at 25%.
    error_rate_alerts: list[dict] = []
    _ALERT_MIN_REQUESTS = 100
    _ALERT_RATE = 0.25
    for pname, pdata in providers_data.items():
        reqs = pdata.get("requests", 0)
        errs = pdata.get("errors", 0)
        rl   = pdata.get("rate_limited", 0)
        # Subtract 429s — they are flow-control, not provider breakage.
        effective_errs = max(0, errs - rl)
        if reqs >= _ALERT_MIN_REQUESTS:
            rate = effective_errs / reqs
            if rate >= _ALERT_RATE:
                error_rate_alerts.append({
                    "provider":     pname,
                    "requests":     reqs,
                    "errors":       effective_errs,
                    "rate_pct":     round(rate * 100, 1),
                    "rate_limited": rl,
                })
    error_rate_alerts.sort(key=lambda x: x["rate_pct"], reverse=True)

    # ── Weekly model health check results (v1.17.0) ───────────────────────
    # Last run is stored at arbiter:health:model:last_run + per-model entries.
    model_health: list[dict] = []
    try:
        last_run = await redis.get("arbiter:health:model:last_run")
        if last_run:
            health_keys = await redis.keys("arbiter:health:model:*:*")
            for k in health_keys:
                if k.endswith(":last_run"):
                    continue
                raw = await redis.get(k)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                except Exception:
                    continue
                # key format: arbiter:health:model:{provider}:{model_id}
                parts = k.split(":", 4)
                if len(parts) >= 5:
                    entry["provider"] = parts[3]
                    entry["model"] = parts[4]
                    model_health.append(entry)
            model_health.sort(key=lambda x: (x.get("status", "") != "ok", x.get("provider", "")))
    except Exception as e:
        logger.debug("model health collection skipped: %s", e)

    # Recent structured errors (from the new error log)
    errors = await obs_stats.get_recent_errors(redis, limit=30)

    # ── Last weekly health summary + rate-limit saturation alerts (#14) ──
    health_summary = {}
    rate_limit_alerts: list[dict] = []
    try:
        raw_sum = await redis.get("arbiter:health:model:last_summary")
        if raw_sum:
            health_summary = json.loads(raw_sum) if isinstance(raw_sum, str) else json.loads(raw_sum.decode())
    except Exception:
        health_summary = {}
    if health_summary:
        rl_by_p = health_summary.get("rate_limited_by_provider") or {}
        # Count probes per provider so we can compute a ratio
        probes_by_p: dict[str, int] = {}
        for entry in model_health:
            probes_by_p[entry["provider"]] = probes_by_p.get(entry["provider"], 0) + 1
        for pname, rl_count in rl_by_p.items():
            total_probes = probes_by_p.get(pname) or rl_count
            if total_probes <= 0:
                continue
            ratio = rl_count / total_probes
            if ratio > 0.5:
                rate_limit_alerts.append({
                    "provider":          pname,
                    "rate_limited":      rl_count,
                    "total_probes":      total_probes,
                    "rate_limited_pct":  round(ratio * 100, 1),
                })
    rate_limit_alerts.sort(key=lambda x: -x["rate_limited_pct"])

    # ── Weekly 7-day log summary (consolidated email, v1.18.0) ───────────
    # Only computed when the report runs on a Monday so subscribers see the
    # weekly recap in the same email that already lands daily at 22:00 IST.
    log_summary_7d = None
    weekly_ai_analysis = None
    try:
        if now.weekday() == 0:  # Monday
            from app.observability import persistent_log as _plog
            log_summary_7d = await _plog.summarise(days=7)
            weekly_ai_analysis = await _generate_weekly_ai_analysis(
                redis, log_summary_7d, errors, model_health,
            )
    except Exception as exc:
        logger.debug("Weekly summary generation failed: %s", exc)

    return {
        # Last 24h
        "day24_requests": day24_requests,
        "day24_success": day24_success,
        "day24_failed": day24_failed,
        "day24_success_rate": round((day24_success / day24_requests * 100) if day24_requests > 0 else 0, 1),
        "day24_top_models": day_top_models,
        "day24_providers": day_provider_counts,
        # Lifetime
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
        "error_rate_alerts": error_rate_alerts,
        "rate_limit_alerts": rate_limit_alerts,
        "model_health": model_health,
        "health_summary": health_summary,
        "log_summary_7d": log_summary_7d,
        "weekly_ai_analysis": weekly_ai_analysis,
        "is_weekly_edition": now.weekday() == 0,
        "generated_at": now.isoformat(),
    }


async def _generate_weekly_ai_analysis(redis, log_summary: dict, errors: list, model_health: list) -> str | None:
    """
    Produce a short AI-written insight paragraph from the past week of
    logs + errors + health probes. Best-effort — returns ``None`` on any
    failure so the email still sends without it.

    Uses Arbiter's own routing layer so the analysis benefits from the
    same provider fallback as every other request.
    """
    try:
        from app.models.schemas import ChatCompletionRequest, Message
        # Local import to avoid a circular import via app.main.
        import app.main as _appmain
        app = getattr(_appmain, "app", None)
        router_instance = getattr(getattr(app, "state", None), "router", None)
        if router_instance is None:
            return None

        top_errors = [
            {
                "provider": e.get("provider"),
                "type":     e.get("type"),
                "msg":      (e.get("msg") or "")[:160],
            }
            for e in (errors or [])[:10]
        ]
        unhealthy = [
            {"provider": m["provider"], "model": m.get("model"), "error": m.get("error", "")[:120]}
            for m in (model_health or [])
            if m.get("status") != "ok"
        ][:10]

        prompt = (
            "You are an SRE assistant analysing a 7-day API gateway report. "
            "In 4–6 short bullet points, surface the most important trends, "
            "risks, and recommended actions. Be concrete and reference the "
            "providers/error categories by name. Avoid generic advice.\n\n"
            f"7-day summary: {json.dumps(log_summary, default=str)[:2000]}\n"
            f"Top recent errors: {json.dumps(top_errors, default=str)[:1500]}\n"
            f"Unhealthy models: {json.dumps(unhealthy, default=str)[:1500]}\n"
        )
        req = ChatCompletionRequest(
            model="auto",
            messages=[Message(role="user", content=prompt)],
            max_tokens=600,
            temperature=0.2,
        )
        resp = await asyncio.wait_for(router_instance.route(req), timeout=45.0)
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return text or None
    except Exception as exc:
        logger.debug("Weekly AI analysis skipped: %s", exc)
        return None


def _classify_error(error: Dict) -> Dict:
    """
    Intelligent failure analysis — classifies an error and produces:
      - severity: critical / warning / temporary / info
      - reason: human-readable WHY this failed
      - recurring: whether this is expected to happen again
      - suggestion: actionable fix or recommendation
    """
    etype = (error.get("type") or "").lower()
    msg = (error.get("msg") or "").lower()
    provider = error.get("provider", "unknown")
    model = error.get("model", "unknown")
    rate_limited = error.get("rate_limited", False)

    # ── Rate limit errors ────────────────────────────────────────────────
    if rate_limited or "rate" in msg or "429" in msg or "quota" in msg:
        return {
            "severity": "temporary",
            "severity_color": "#f59e0b",
            "reason": f"Rate limit hit on {provider} for model {model}",
            "recurring": "Yes — expected during high traffic or with free-tier API keys",
            "suggestion": (
                "Add more API keys for this provider in Settings → Keys to increase "
                "throughput, or upgrade to a paid tier. Arbiter auto-rotates keys so "
                "this resolves itself once the cooldown expires (usually 60s)."
            ),
            "fixable": "No code fix needed — operational limit",
        }

    # ── Auth / key errors ────────────────────────────────────────────────
    if any(kw in msg for kw in ["401", "unauthorized", "invalid key", "invalid api",
                                  "authentication", "api key", "invalid_api_key"]):
        return {
            "severity": "critical",
            "severity_color": "#ef4444",
            "reason": f"Authentication failure on {provider}/{model} — API key rejected",
            "recurring": "Yes — will persist until the key is replaced or re-enabled",
            "suggestion": (
                "The API key for this provider is likely expired, revoked, or has "
                "hit its billing limit. Go to Settings → Keys → {provider} and "
                "regenerate or replace the key. Check the provider's dashboard for "
                "account status.".replace("{provider}", provider)
            ),
            "fixable": "Replace/regenerate the API key",
        }

    # ── Access denied ────────────────────────────────────────────────────
    if any(kw in msg for kw in ["403", "forbidden", "access denied", "permission"]):
        return {
            "severity": "critical",
            "severity_color": "#ef4444",
            "reason": f"Access denied by {provider} for model {model}",
            "recurring": "Yes — indicates an account/model permission issue",
            "suggestion": (
                "The API key does not have permission to use this model. This can "
                "happen if the model requires a paid plan, is in preview/beta, or "
                "the account was suspended. Check the provider console and verify "
                "model access for this key."
            ),
            "fixable": "Enable model access in provider dashboard or use a paid-tier key",
        }

    # ── Model not found ──────────────────────────────────────────────────
    if any(kw in msg for kw in ["not found", "404", "model not found", "does not exist",
                                  "invalid model", "unknown model", "deprecated"]):
        return {
            "severity": "warning",
            "severity_color": "#f59e0b",
            "reason": f"Model '{model}' not found on {provider}",
            "recurring": "Likely permanent — model may have been deprecated or renamed",
            "suggestion": (
                "This model ID may have been deprecated or renamed by the provider. "
                "Check the provider's model catalogue for the current name. If the model "
                "was removed, disable it in Settings → Models to stop routing attempts. "
                "Consider updating the model mapping in the provider configuration."
            ),
            "fixable": "Update model name in config or disable the model",
        }

    # ── Timeout / network ────────────────────────────────────────────────
    if any(kw in msg for kw in ["timeout", "timed out", "deadline exceeded",
                                  "connection", "network", "dns", "refused",
                                  "reset by peer", "broken pipe"]):
        return {
            "severity": "temporary",
            "severity_color": "#6b7280",
            "reason": f"Network/timeout error connecting to {provider}",
            "recurring": "Usually transient — resolves within minutes",
            "suggestion": (
                "This is typically a momentary network issue or the provider's "
                "infrastructure was briefly overloaded. Arbiter's fallback chain "
                "automatically routes to the next provider. If this persists for "
                "the same provider, check their status page or add latency monitoring."
            ),
            "fixable": "No code fix — transient infrastructure issue",
        }

    # ── Upstream server errors ───────────────────────────────────────────
    if any(kw in msg for kw in ["500", "502", "503", "504", "internal server error",
                                  "bad gateway", "service unavailable", "overloaded"]):
        return {
            "severity": "temporary",
            "severity_color": "#6b7280",
            "reason": f"Upstream server error from {provider} ({model})",
            "recurring": "Usually temporary — provider-side issue",
            "suggestion": (
                "The provider's backend returned a server error. This is outside "
                "Arbiter's control and typically resolves on its own. Arbiter's "
                "fallback routing ensures user requests still complete via another "
                "provider. No action needed unless this happens frequently for "
                "the same provider."
            ),
            "fixable": "No code fix — provider-side issue",
        }

    # ── Content/safety filter ────────────────────────────────────────────
    if any(kw in msg for kw in ["safety", "content filter", "blocked", "harmful",
                                  "policy", "violation", "moderation"]):
        return {
            "severity": "info",
            "severity_color": "#3b82f6",
            "reason": f"Content filtered by {provider}'s safety system",
            "recurring": "Expected — depends on input content",
            "suggestion": (
                "The provider's safety system blocked this request. This is normal "
                "behaviour for certain input types. No action needed — user gets "
                "an appropriate error message. If this happens excessively, review "
                "whether upstream clients are sending problematic prompts."
            ),
            "fixable": "Expected behaviour — no fix needed",
        }

    # ── Token/context length exceeded ────────────────────────────────────
    if any(kw in msg for kw in ["token", "context length", "max_tokens",
                                  "too long", "exceeds", "context window"]):
        return {
            "severity": "warning",
            "severity_color": "#f59e0b",
            "reason": f"Token/context limit exceeded on {provider}/{model}",
            "recurring": "Depends on user input length",
            "suggestion": (
                "The request exceeded the model's context window. Consider routing "
                "to models with larger context windows (e.g. Gemini 1M, GPT-4 128K) "
                "or implementing prompt truncation in the client. You can also set "
                "max_tokens in the request to stay within limits."
            ),
            "fixable": "Client-side: truncate input or use larger-context model",
        }

    # ── Billing / quota exhausted ────────────────────────────────────────
    if any(kw in msg for kw in ["billing", "payment", "insufficient", "credit",
                                  "spend limit", "hard limit"]):
        return {
            "severity": "critical",
            "severity_color": "#ef4444",
            "reason": f"Billing/quota exhausted on {provider}",
            "recurring": "Yes — until credits are added or billing is resolved",
            "suggestion": (
                "The API key has exhausted its billing quota or credits. Add funds "
                "to the provider account or switch to another key. Free-tier keys "
                "have daily limits that reset at midnight (provider time)."
            ),
            "fixable": "Add credits or replace key",
        }

    # ── Fallback: unclassified ───────────────────────────────────────────
    return {
        "severity": "unknown",
        "severity_color": "#6b7280",
        "reason": f"Unclassified error on {provider}/{model}: {etype}",
        "recurring": "Unknown — requires manual review",
        "suggestion": (
            "Review the full error message in the Logs page (/logs). If this error "
            "persists, it may indicate a bug in the provider adapter or an API "
            "change that needs a code update. Check the provider's changelog."
        ),
        "fixable": "Requires manual investigation",
    }


def _build_report_html(stats: Dict, token_names: Dict[str, str]) -> str:
    """Generate the HTML email body from collected stats."""
    now_ist = datetime.now(_IST).strftime("%B %d, %Y — %I:%M %p IST")

    # --- 24h model rows ---
    day_model_rows = ""
    for i, (model, count) in enumerate(stats["day24_top_models"], 1):
        pct = round(count / stats["day24_requests"] * 100, 1) if stats["day24_requests"] > 0 else 0
        day_model_rows += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">#{i}</td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:12px">{model}</td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{count:,}</td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{pct}%</td></tr>'

    # --- 24h provider rows ---
    day_provider_rows = ""
    for pname, reqs in sorted(stats["day24_providers"].items(), key=lambda x: x[1], reverse=True):
        day_provider_rows += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{pname}</td><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{reqs:,}</td></tr>'

    # --- Lifetime provider health rows ---
    provider_rows = ""
    for pname, pdata in sorted(stats["providers"].items()):
        reqs = pdata["requests"]
        errs = pdata["errors"]
        health_pct = round(((reqs - errs) / reqs * 100) if reqs > 0 else 100, 1)
        color = "#10b981" if health_pct >= 95 else "#f59e0b" if health_pct >= 80 else "#ef4444"
        arrow = "↑" if health_pct >= 95 else "↓"
        provider_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{pname}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600">{health_pct}% {arrow}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{reqs:,}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#ef4444">{errs}</td></tr>'

    # --- Lifetime top models rows ---
    model_rows = ""
    for i, (model, count) in enumerate(stats["top_models"], 1):
        pct = round(count / stats["total_requests"] * 100, 1) if stats["total_requests"] > 0 else 0
        model_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">#{i}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:12px">{model}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{count:,}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{pct}%</td></tr>'

    # --- Top gateways rows ---
    gateway_rows = ""
    for i, (tid, count) in enumerate(stats["top_tokens"], 1):
        name = token_names.get(tid, tid[:12] + "…")
        gateway_rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">#{i}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{name}</td><td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{count:,}</td></tr>'

    # Error analysis section — AI-powered failure diagnostics
    error_section = ""
    if stats["errors"]:
        # Group errors by (provider, type) to avoid repetition
        seen_groups = {}
        for err in stats["errors"]:
            group_key = f"{err.get('provider','?')}|{err.get('type','?')}"
            if group_key not in seen_groups:
                seen_groups[group_key] = {"error": err, "count": 1}
            else:
                seen_groups[group_key]["count"] += 1

        error_cards = ""
        for group_key, group in sorted(seen_groups.items(), key=lambda x: x[1]["count"], reverse=True)[:10]:
            err = group["error"]
            count = group["count"]
            analysis = _classify_error(err)
            sev = analysis["severity"]
            sev_color = analysis["severity_color"]
            provider = err.get("provider", "unknown")
            model = err.get("model", "unknown")
            msg_preview = _sanitise_for_email(err.get("msg"), max_len=160)
            provider_s = _sanitise_for_email(provider, max_len=40)
            model_s    = _sanitise_for_email(model, max_len=80)

            error_cards += f"""
            <div style="margin-bottom:14px;padding:14px;border:1px solid #e5e7eb;border-radius:8px;border-left:4px solid {sev_color};background:#fafafa">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <span style="font-size:12px;font-weight:700;color:{sev_color};text-transform:uppercase">{sev}</span>
                <span style="font-size:11px;color:#888;background:#f1f5f9;padding:2px 8px;border-radius:4px">{count}× in last 48h</span>
              </div>
              <div style="font-size:13px;font-weight:600;color:#111;margin-bottom:4px">{analysis['reason']}</div>
              <div style="font-size:11px;color:#666;font-family:monospace;margin-bottom:8px;word-break:break-all">{msg_preview}</div>
              <table style="width:100%;font-size:11px;color:#444;border-collapse:collapse">
                <tr><td style="padding:3px 0;font-weight:600;width:120px">Will recur?</td><td>{analysis['recurring']}</td></tr>
                <tr><td style="padding:3px 0;font-weight:600">Fix available?</td><td>{analysis['fixable']}</td></tr>
                <tr><td style="padding:3px 0;font-weight:600;vertical-align:top">Suggestion</td><td style="padding:3px 0">{analysis['suggestion']}</td></tr>
              </table>
            </div>"""

        error_section = f"""
        <div style="margin-top:28px;margin-bottom:24px">
          <h3 style="margin:0 0 14px;font-size:17px;color:#111">🔍 Failure Analysis</h3>
          <p style="font-size:12px;color:#666;margin:0 0 14px">Intelligent diagnostics for recent failures — grouped by provider and error type.</p>
          {error_cards}
        </div>"""

    # --- High error-rate alerts banner (refined v1.18.0) ---
    alerts_section = ""
    alerts = stats.get("error_rate_alerts") or []
    if alerts:
        rows = ""
        for a in alerts:
            rows += (
                f'<tr><td style="padding:6px 12px;border-bottom:1px solid #fecaca;font-weight:600">{_sanitise_for_email(a["provider"], max_len=40)}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #fecaca;color:#b91c1c;font-weight:700">{a["rate_pct"]}%</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #fecaca">{a["errors"]:,} / {a["requests"]:,}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #fecaca">{a["rate_limited"]:,}</td></tr>'
            )
        alerts_section = f"""
        <div style="margin-top:18px;margin-bottom:24px;border:1px solid #fca5a5;background:#fef2f2;border-radius:10px;padding:14px 16px">
          <h3 style="margin:0 0 8px;font-size:16px;color:#991b1b">⚠️ High Error-Rate Providers</h3>
          <p style="font-size:12px;color:#7f1d1d;margin:0 0 10px">Providers with non-429 error rate ≥ 25% over ≥100 requests — evaluate whether to keep or remove.</p>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border-radius:6px;overflow:hidden">
            <tr style="background:#fee2e2"><th style="padding:6px 12px;text-align:left">Provider</th><th style="padding:6px 12px;text-align:left">Error Rate</th><th style="padding:6px 12px;text-align:left">Errors / Total</th><th style="padding:6px 12px;text-align:left">Rate-Limited (excluded)</th></tr>
            {rows}
          </table>
        </div>"""

    # --- Rate-limit saturation alerts (v1.18.0) ---
    rl_alerts_section = ""
    rl_alerts = stats.get("rate_limit_alerts") or []
    if rl_alerts:
        rows = ""
        for a in rl_alerts:
            rows += (
                f'<tr><td style="padding:6px 12px;border-bottom:1px solid #fde68a;font-weight:600">{_sanitise_for_email(a["provider"], max_len=40)}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #fde68a;color:#a16207;font-weight:700">{a["rate_limited_pct"]}%</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #fde68a">{a["rate_limited"]} / {a["total_probes"]}</td></tr>'
            )
        rl_alerts_section = f"""
        <div style="margin-top:18px;margin-bottom:24px;border:1px solid #fcd34d;background:#fffbeb;border-radius:10px;padding:14px 16px">
          <h3 style="margin:0 0 8px;font-size:16px;color:#92400e">⏳ Rate-Limit Saturation</h3>
          <p style="font-size:12px;color:#78350f;margin:0 0 10px">More than half of the weekly health probes for these providers were rate-limited — consider adding more keys or shifting priority.</p>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border-radius:6px;overflow:hidden">
            <tr style="background:#fef3c7"><th style="padding:6px 12px;text-align:left">Provider</th><th style="padding:6px 12px;text-align:left">Probes Rate-Limited</th><th style="padding:6px 12px;text-align:left">Count</th></tr>
            {rows}
          </table>
        </div>"""

    # --- Weekly model health section (v1.17.0) ---
    health_section = ""
    health = stats.get("model_health") or []
    if health:
        bad = [h for h in health if h.get("status") != "ok"]
        ok_count = len(health) - len(bad)
        rows = ""
        # Show all failing models first; summarize ok count
        for h in bad[:30]:
            rows += (
                f'<tr><td style="padding:5px 10px;border-bottom:1px solid #fed7aa">{_sanitise_for_email(h.get("provider",""), max_len=40)}</td>'
                f'<td style="padding:5px 10px;border-bottom:1px solid #fed7aa;font-family:monospace;font-size:11px">{_sanitise_for_email(h.get("model",""), max_len=80)}</td>'
                f'<td style="padding:5px 10px;border-bottom:1px solid #fed7aa;color:#b91c1c;font-weight:600">{_sanitise_for_email(h.get("status",""), max_len=20)}</td>'
                f'<td style="padding:5px 10px;border-bottom:1px solid #fed7aa;font-size:11px;color:#7f1d1d">{_sanitise_for_email(h.get("error",""), max_len=120)}</td></tr>'
            )
        health_section = f"""
        <div style="margin-top:18px;margin-bottom:24px;border:1px solid #fdba74;background:#fff7ed;border-radius:10px;padding:14px 16px">
          <h3 style="margin:0 0 8px;font-size:16px;color:#9a3412">🩺 Weekly Model Health Check</h3>
          <p style="font-size:12px;color:#7c2d12;margin:0 0 10px">{ok_count} working · {len(bad)} failing. Update the model catalogue or rotate keys for failing entries.</p>
          {"" if not bad else f'<table style="width:100%;border-collapse:collapse;font-size:12px;background:#fff;border-radius:6px;overflow:hidden"><tr style="background:#ffedd5"><th style="padding:5px 10px;text-align:left">Provider</th><th style="padding:5px 10px;text-align:left">Model</th><th style="padding:5px 10px;text-align:left">Status</th><th style="padding:5px 10px;text-align:left">Error</th></tr>{rows}</table>'}
        </div>"""

    # --- Weekly 7-day log summary + AI analysis (v1.18.0, Mondays only) ---
    weekly_section = ""
    log_summary = stats.get("log_summary_7d")
    ai_text     = stats.get("weekly_ai_analysis")
    if log_summary:
        prov_rows = ""
        for p, c in (log_summary.get("api_by_provider") or {}).items():
            prov_rows += (
                f'<tr><td style="padding:5px 10px;border-bottom:1px solid #ddd6fe">{_sanitise_for_email(p, max_len=40)}</td>'
                f'<td style="padding:5px 10px;border-bottom:1px solid #ddd6fe">{int(c):,}</td></tr>'
            )
        err_rows = ""
        for cat, c in (log_summary.get("err_by_category") or {}).items():
            err_rows += (
                f'<tr><td style="padding:5px 10px;border-bottom:1px solid #ddd6fe">{_sanitise_for_email(cat, max_len=40)}</td>'
                f'<td style="padding:5px 10px;border-bottom:1px solid #ddd6fe">{int(c):,}</td></tr>'
            )
        ai_html = ""
        if ai_text:
            safe_ai = _sanitise_for_email(ai_text, max_len=2000)
            safe_ai = safe_ai.replace("\\n", "<br>").replace("\n", "<br>")
            ai_html = (
                '<div style="margin-top:14px;padding:12px 14px;background:#fff;border:1px solid #c7d2fe;border-radius:8px">'
                '<div style="font-size:11px;font-weight:700;color:#4f46e5;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px">AI Analysis</div>'
                f'<div style="font-size:13px;line-height:1.55;color:#1e1b4b">{safe_ai}</div>'
                '</div>'
            )
        weekly_section = f"""
        <div style="margin-top:18px;margin-bottom:24px;border:1px solid #c7d2fe;background:#eef2ff;border-radius:10px;padding:14px 16px">
          <h3 style="margin:0 0 8px;font-size:16px;color:#3730a3">📅 Weekly Summary (last 7 days)</h3>
          <p style="font-size:12px;color:#312e81;margin:0 0 10px">
            {int(log_summary.get('api_total', 0)):,} API calls · {log_summary.get('api_error_rate_pct', 0)}% error rate ·
            p50 {int(log_summary.get('p50_latency_ms', 0))}ms · p95 {int(log_summary.get('p95_latency_ms', 0))}ms ·
            {int(log_summary.get('activity_count', 0))} admin changes
          </p>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <div style="flex:1;min-width:200px">
              <h4 style="margin:0 0 6px;font-size:12px;color:#4338ca">Calls by Provider</h4>
              <table style="width:100%;border-collapse:collapse;font-size:12px;background:#fff;border-radius:6px;overflow:hidden">
                {prov_rows or '<tr><td style="padding:6px 10px;color:#999">No data</td></tr>'}
              </table>
            </div>
            <div style="flex:1;min-width:200px">
              <h4 style="margin:0 0 6px;font-size:12px;color:#4338ca">Errors by Category</h4>
              <table style="width:100%;border-collapse:collapse;font-size:12px;background:#fff;border-radius:6px;overflow:hidden">
                {err_rows or '<tr><td style="padding:6px 10px;color:#999">No errors</td></tr>'}
              </table>
            </div>
          </div>
          {ai_html}
        </div>"""

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;background:#fff">
  <div style="text-align:center;margin-bottom:24px;padding:20px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;color:#fff">
    <h1 style="margin:0 0 4px;font-size:22px">📊 Arbiter Daily Report</h1>
    <p style="margin:0;font-size:13px;opacity:.85">{now_ist}{' · Weekly edition' if stats.get('is_weekly_edition') else ''}</p>
  </div>

  <!-- ═══════ LAST 24 HOURS ═══════ -->
  <div style="margin-bottom:28px;padding:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px">
    <h2 style="margin:0 0 14px;font-size:17px;color:#334155">⏱️ Last 24 Hours</h2>
    <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:120px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#0f172a">{stats['day24_requests']:,}</div>
        <div style="font-size:10px;color:#64748b;margin-top:4px">Requests</div>
      </div>
      <div style="flex:1;min-width:120px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#1e40af">{stats['day24_success_rate']}%</div>
        <div style="font-size:10px;color:#64748b;margin-top:4px">Success Rate</div>
      </div>
      <div style="flex:1;min-width:120px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#dc2626">{stats['day24_failed']:,}</div>
        <div style="font-size:10px;color:#64748b;margin-top:4px">Failed</div>
      </div>
    </div>

    <!-- 24h Top Models -->
    <div style="margin-bottom:12px">
      <h4 style="margin:0 0 8px;font-size:13px;color:#475569">Top Models (Today)</h4>
      <table style="width:100%;border-collapse:collapse;font-size:12px;background:#fff;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden">
        <thead><tr style="background:#f1f5f9"><th style="padding:6px 10px;text-align:left">#</th><th style="padding:6px 10px;text-align:left">Model</th><th style="padding:6px 10px;text-align:left">Reqs</th><th style="padding:6px 10px;text-align:left">Share</th></tr></thead>
        <tbody>{day_model_rows if day_model_rows else '<tr><td colspan="4" style="padding:12px;text-align:center;color:#999">No requests today yet</td></tr>'}</tbody>
      </table>
    </div>

    <!-- 24h Providers -->
    <div>
      <h4 style="margin:0 0 8px;font-size:13px;color:#475569">Provider Usage (Today)</h4>
      <table style="width:100%;border-collapse:collapse;font-size:12px;background:#fff;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden">
        <thead><tr style="background:#f1f5f9"><th style="padding:6px 10px;text-align:left">Provider</th><th style="padding:6px 10px;text-align:left">Requests</th></tr></thead>
        <tbody>{day_provider_rows if day_provider_rows else '<tr><td colspan="2" style="padding:12px;text-align:center;color:#999">No provider data today</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <!-- ═══════ ALL TIME (LIFETIME) ═══════ -->
  <h2 style="margin:0 0 14px;font-size:17px;color:#334155">📈 All Time</h2>
  <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
    <div style="flex:1;min-width:130px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:#166534">{stats['total_requests']:,}</div>
      <div style="font-size:10px;color:#166534;margin-top:4px">Total Requests</div>
    </div>
    <div style="flex:1;min-width:130px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:#1e40af">{stats['success_rate']}%</div>
      <div style="font-size:10px;color:#1e40af;margin-top:4px">Success Rate</div>
    </div>
    <div style="flex:1;min-width:130px;background:#fef3c7;border:1px solid #fde68a;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:#92400e">{stats['cache_hit_rate']}%</div>
      <div style="font-size:10px;color:#92400e;margin-top:4px">Cache Hit Rate</div>
    </div>
    <div style="flex:1;min-width:130px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:#991b1b">{stats['failed']:,}</div>
      <div style="font-size:10px;color:#991b1b;margin-top:4px">Failed</div>
    </div>
  </div>

  <!-- Top 5 Models (Lifetime) -->
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

  <!-- Provider Health (Lifetime) -->
  <div style="margin-bottom:24px">
    <h3 style="margin:0 0 12px;font-size:16px;color:#111">🏥 Provider Health</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
      <thead><tr style="background:#f8f9fa"><th style="padding:8px 12px;text-align:left">Provider</th><th style="padding:8px 12px;text-align:left">Health</th><th style="padding:8px 12px;text-align:left">Requests</th><th style="padding:8px 12px;text-align:left">Errors</th></tr></thead>
      <tbody>{provider_rows if provider_rows else '<tr><td colspan="4" style="padding:16px;text-align:center;color:#999">No provider data yet</td></tr>'}</tbody>
    </table>
  </div>

  {alerts_section}
  {rl_alerts_section}
  {health_section}
  {weekly_section}
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
        subject = f"📊 Arbiter Daily Report — {datetime.now(_IST).strftime('%b %d, %Y')}"

        sent = await email_service.send_to_admin(subject, html)
        if sent:
            logger.info("Daily analytics report sent successfully")
        return sent

    except Exception as e:
        logger.error("Failed to generate/send daily report: %s", e, exc_info=True)
        return False


async def _scheduler_loop(app):
    """Background loop that sends the report at 22:00 IST (16:30 UTC) daily."""
    report_hour = settings.DAILY_REPORT_HOUR      # 16 (UTC)
    report_minute = settings.DAILY_REPORT_MINUTE  # 30 (UTC)
    logger.info(
        "Daily report scheduler started (sends at %02d:%02d UTC / 22:00 IST)",
        report_hour, report_minute,
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            # Calculate seconds until next report time
            target = now.replace(hour=report_hour, minute=report_minute, second=0, microsecond=0)
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
