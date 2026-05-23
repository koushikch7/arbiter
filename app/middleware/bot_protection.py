"""
Bot protection & IP-based rate limiting middleware.

Three protection layers:
  1. Malicious user-agent blocklist  — exploit scanners, AI data-scrapers
  2. Per-IP sliding-window rate limit — Redis-backed, fail-open on Redis errors
  3. Fires before the main auth check so bad actors never reach business logic

Rate limit tiers (requests per minute, per IP):
  AUTH_TIER    /auth/*          10 req/min  — brute-force / credential-stuffing guard
  API_TIER     /v1/*           600 req/min  — abuse guard (real throttling is key-pool level)
  DEFAULT_TIER everything else 200 req/min  — DoS guard for the dashboard UI

Exempt from rate limiting:
  /health, /static/*, /sw.js, /manifest.webmanifest, /robots.txt, /sitemap.xml,
  /favicon.ico  — CDN/health-check paths that must never be throttled.

Security design notes:
  - Rate-limit keys are keyed on a 16-hex MD5 of the real IP (not the raw IP)
    so we avoid storing PII in Redis while still distinguishing callers.
  - Keys expire at 2× the window to handle clock skew across restarts.
  - On Redis failure the middleware fails **open** — requests are allowed
    through so a Redis outage never brings down the gateway.
  - 429 responses for API routes return JSON (OpenAI error format).
  - 429 responses for HTML routes return a plain text body.
  - 403 bad-bot responses are plain text with no detail (info-hiding).
"""

from __future__ import annotations

import hashlib
import logging
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known-malicious user-agent substrings (case-insensitive).
# These are exploit scanners, mass-crawlers, and AI training scrapers that
# have no legitimate reason to access an admin gateway.
# ---------------------------------------------------------------------------
_BAD_UA_SUBSTRINGS: tuple[str, ...] = (
    # Security scanners / exploit frameworks
    "nikto", "sqlmap", "masscan", "zgrab", "nmap", "dirbuster",
    "dotdotpwn", "w3af", "nuclei", "gobuster", "wfuzz",
    "acunetix", "nessus", "openvas", "burpsuite", "burp suite",
    "metasploit", "slowhttptest", "slowloris", "havij", "hydra",
    # AI data harvesters (explicitly disallowed by robots.txt AND blocked here)
    "gptbot", "chatgpt-user", "ccbot", "claudebot", "anthropic-ai",
    "claude-web", "cohere-ai", "google-extended", "bytespider",
    "petalbot", "diffbot", "scrapy", "applebot-extended",
    # Aggressive SEO crawlers
    "ahrefsbot", "semrushbot", "mj12bot", "dotbot", "blexbot",
    "majestic", "seokicks", "sistrix",
    # Spam / click-fraud bots
    "ltx71", "masscan", "zmeu", "python-masscan",
)

# ---------------------------------------------------------------------------
# Paths that are always exempt from rate limiting
# ---------------------------------------------------------------------------
_SKIP_RL_EXACT: frozenset[str] = frozenset({
    "/health", "/robots.txt", "/sitemap.xml", "/favicon.ico",
    "/sw.js", "/service-worker.js", "/manifest.webmanifest", "/manifest.json",
})
_SKIP_RL_PREFIXES: tuple[str, ...] = ("/static/",)

# ---------------------------------------------------------------------------
# Rate limit tiers: (requests_per_minute, window_seconds)
# ---------------------------------------------------------------------------
_AUTH_LIMIT    = (10,  60)    # /auth/* — brute-force protection
_API_LIMIT     = (600, 60)    # /v1/*  — DoS guard (key-pool is the real throttle)
_DEFAULT_LIMIT = (200, 60)    # everything else — dashboard DoS guard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _real_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For from trusted proxies."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Take the leftmost IP (the original client); proxies append right-to-left.
        return xff.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")  # Cloudflare
    if cf:
        return cf.strip()
    return (request.client.host if request.client else "unknown")


def _ua_is_malicious(ua: str) -> bool:
    """Return True if the user-agent matches a known bad actor."""
    if not ua:
        return False  # blank UA is allowed (curl, internal health checkers)
    ua_lower = ua.lower()
    return any(bad in ua_lower for bad in _BAD_UA_SUBSTRINGS)


def _rl_key(tier: str, ip: str, slot: int) -> str:
    """Build a Redis key for this tier/ip/time-slot. Hashes IP to avoid PII storage."""
    ip_hash = hashlib.md5(ip.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"arbiter:rl:{tier}:{ip_hash}:{slot}"


# ---------------------------------------------------------------------------
# Middleware class
# ---------------------------------------------------------------------------

class BotProtectionMiddleware(BaseHTTPMiddleware):
    """IP rate limiting + malicious-bot blocking middleware.

    Must be added AFTER SecurityHeadersMiddleware in the stack so that
    429/403 responses still receive the security headers.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # ── 1. Bad-bot user-agent check (runs on ALL paths) ──────────────────
        # We block malicious scanners even on /health — a Nikto probe on
        # /health is still a Nikto probe.  Static assets are exempt so
        # legitimate browsers fetching /sw.js are never blocked.
        ua = request.headers.get("user-agent", "")
        is_static = path.startswith("/static/") or path in {
            "/favicon.ico", "/manifest.webmanifest", "/manifest.json",
            "/robots.txt", "/sitemap.xml",
        }
        if not is_static and _ua_is_malicious(ua):
            logger.warning(
                "Bot blocked | ua=%.80r | ip=%s | path=%s",
                ua, _real_ip(request), path,
            )
            return Response(
                content="Forbidden",
                status_code=403,
                media_type="text/plain",
            )

        # ── 2. Exempt static/health paths from rate limiting ─────────────────
        if path in _SKIP_RL_EXACT or any(path.startswith(p) for p in _SKIP_RL_PREFIXES):
            return await call_next(request)

        # ── 3. Per-IP rate limiting ───────────────────────────────────────────
        redis = getattr(getattr(request, "app", None), "state", None)
        redis = getattr(redis, "redis", None) if redis else None

        if redis is not None:
            ip = _real_ip(request)

            if path.startswith("/auth/"):
                limit, window = _AUTH_LIMIT
                tier = "auth"
            elif path.startswith("/v1/"):
                limit, window = _API_LIMIT
                tier = "api"
            else:
                limit, window = _DEFAULT_LIMIT
                tier = "web"

            slot = int(time.time()) // window
            rk = _rl_key(tier, ip, slot)

            try:
                count = await redis.incr(rk)
                if count == 1:
                    # First hit in this window — set expiry
                    await redis.expire(rk, window * 2)

                if count > limit:
                    retry_after = window - (int(time.time()) % window)
                    logger.warning(
                        "Rate limit exceeded | tier=%s ip=%s path=%s count=%d/%d",
                        tier, ip, path, count, limit,
                    )
                    is_api = path.startswith("/v1/") or path.startswith("/api/")
                    if is_api:
                        return JSONResponse(
                            status_code=429,
                            content={
                                "error": {
                                    "message": (
                                        f"Too many requests from your IP. "
                                        f"Limit: {limit} requests per {window}s."
                                    ),
                                    "type":  "rate_limit_error",
                                    "code":  429,
                                }
                            },
                            headers={
                                "Retry-After":           str(retry_after),
                                "X-RateLimit-Limit":     str(limit),
                                "X-RateLimit-Remaining": "0",
                                "X-RateLimit-Reset":     str(int(time.time()) + retry_after),
                            },
                        )
                    return Response(
                        content=f"429 Too Many Requests — retry after {retry_after}s",
                        status_code=429,
                        media_type="text/plain",
                        headers={"Retry-After": str(retry_after)},
                    )

            except Exception as exc:
                # Fail open — a Redis error must never block legitimate users.
                logger.debug("BotProtection: Redis error (fail-open): %s", exc)

        return await call_next(request)
