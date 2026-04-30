"""
Authentication & authorization middleware for Arbiter.

Two-tier auth model
-------------------

Arbiter exposes two classes of routes with different audiences and
therefore different auth requirements:

1. **OpenAI-compatible API routes** — ``/v1/chat/completions``,
   ``/v1/images/generations``, ``/v1/models``, ``/v1/images/models``.
   These are called by OpenAI SDK clients and downstream tools, so they
   authenticate with ``Authorization: Bearer <gateway-token>``.

2. **Admin / UI routes** — everything else (dashboard, settings, logs,
   ``/api/*`` admin endpoints). These are used by humans in a
   browser and authenticate via a signed Google-SSO session cookie
   (see ``app/auth/sso.py``).

If Google SSO is not configured, the UI routes fall through to the
gateway-token check (so the original single-auth-layer mode still works).

Middlewares (outer → inner)
---------------------------

::

    SecurityHeadersMiddleware  ← adds X-Frame-Options, CSP, etc.
    CORSMiddleware             ← allowlist from ALLOWED_CORS_ORIGINS
    SessionMiddleware          ← Starlette signed cookie (outside our code)
    CloudflareAccessMiddleware ← optional, validates CF Access JWT
    GatewayAuthMiddleware      ← routes /v1/* bearer + UI session check
    (request timing, last)

Returns 401 JSON / redirect to /login on auth failure, 403 JSON on
authorization failure.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import List, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------
# Paths that bypass ALL auth (health checks, auth endpoints, static assets,
# Swagger docs, public login page).
_ALWAYS_OPEN: frozenset = frozenset([
    "/health",
    "/",  # redirects to /dashboard, auth enforced on target
    "/docs", "/redoc", "/openapi.json",
    "/login",
    "/favicon.ico",
])

_ALWAYS_OPEN_PREFIXES: tuple = (
    "/static/",
    "/auth/",   # /auth/login, /auth/callback, /auth/me, /auth/logout, /auth/pending, /auth/config
)

# OpenAI-compatible API routes — authenticate with Bearer token ONLY
# (session cookie is NOT accepted here; OpenAI SDK clients don't send
# cookies, and mixing auth schemes confuses tooling).
_BEARER_ONLY_PREFIXES: tuple = (
    "/v1/",
)


def _error_401(message: str = "Invalid API key") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"message": message, "type": "authentication_error",
                           "code": 401}},
    )


def _error_403(message: str = "Access denied") -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"error": {"message": message, "type": "authorization_error",
                           "code": 403}},
    )


def _wants_json(request: Request) -> bool:
    """Heuristic: does the caller want a JSON response or an HTML redirect?"""
    accept = (request.headers.get("accept") or "").lower().strip()
    if "text/html" in accept:
        return False
    if "application/json" in accept or accept in ("", "*/*"):
        return True
    # API paths always get JSON
    if request.url.path.startswith(("/api/", "/v1/", "/auth/", "/logs/",
                                    "/settings/", "/modal/",
                                    "/cloudflare/", "/dashboard/stats")):
        return True
    return False


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds standard security headers to every response.

    CSP is kept relatively permissive because Arbiter's UI loads a few CDNs
    (Chart.js, marked.js, tailwind) and uses inline styles/scripts. Tighten
    further by pinning CDN hashes if desired.
    """

    def __init__(self, app, *, allow_iframe: bool = False):
        super().__init__(app)
        self._allow_iframe = allow_iframe

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if not self._allow_iframe:
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        # Prevent CDN/proxy caching of API and auth responses.  /auth/me in
        # particular returns the signed-in user's email; if a CDN caches it,
        # the next *anonymous* visitor (e.g. an incognito tab) would see
        # somebody else's identity on the login page.
        # HTML pages already set their own no-store via dashboard.py.
        path = request.url.path
        if (path.startswith("/api/")
                or path.startswith("/auth/")
                or path == "/login"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["CDN-Cache-Control"] = "no-store"
            response.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("Vary", "Cookie")
        # Content Security Policy — allow our CDNs + inline (the UI uses a
        # lot of onclick handlers and inline styles).
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
            "  https://cdn.jsdelivr.net https://cdn.tailwindcss.com "
            "  https://unpkg.com https://accounts.google.com "
            "  https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "  https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https: ; "
            "connect-src 'self' https://api.openai.com https://accounts.google.com "
            "  https://cloudflareinsights.com; "
            "frame-src 'self' https://accounts.google.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self' https://accounts.google.com",
        )
        return response


# ---------------------------------------------------------------------------
# Gateway auth — dual-mode
# ---------------------------------------------------------------------------


class GatewayAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce authentication on all non-exempt paths.

    Mode selection per-request:

    * ``/v1/*``         → Bearer token (OpenAI SDK compatible).
    * Everything else   → Google session cookie (if SSO configured),
                          else Bearer token (legacy single-auth mode).
    """

    def __init__(self, app, allowed_keys: List[str], sso_enabled: bool = False,
                 require_auth: bool = False):
        super().__init__(app)
        self._allowed = frozenset(k.strip() for k in allowed_keys if k.strip())
        self._sso_enabled = sso_enabled
        # When True, /v1/* refuses requests if no gateway keys/tokens are
        # configured. This is fail-closed mode — the gateway never makes an
        # outbound LLM call without a valid Bearer token.
        self._require_auth = require_auth

    # --- internals ---
    def _effective_keys(self, request: Request) -> frozenset:
        dynamic: frozenset = frozenset()
        try:
            dset = getattr(request.app.state, "gateway_tokens", None)
            if dset:
                dynamic = frozenset(dset)
        except Exception:
            pass
        return self._allowed | dynamic

    def _check_bearer(self, request: Request, keys: frozenset) -> bool:
        """
        Validate the Bearer token. Side effect: when the token matches a
        named gateway token (registered via /api/gateway/tokens), attach
        ``request.state.gateway_token_id`` and ``…_token_name`` so downstream
        observability can attribute the request.
        """
        if not keys:
            return True  # auth disabled entirely (legacy mode)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[len("Bearer "):].strip()
        if token not in keys:
            return False
        # Identify which named token (if any) was used
        meta = getattr(request.app.state, "gateway_token_meta", {}) or {}
        info = meta.get(token)
        if info:
            request.state.gateway_token_id = info.get("id")
            request.state.gateway_token_name = info.get("name")
        else:
            # env-var key — bucket under a synthetic id
            request.state.gateway_token_id = "env"
            request.state.gateway_token_name = "env-var"
        return True

    def _check_session(self, request: Request) -> tuple[bool, str]:
        """Return (ok, reason). ok=True means user is approved."""
        # Import lazily to avoid circular import at module load
        from app.auth.sso import get_session_user
        user = get_session_user(request)
        if user is None:
            return False, "not_logged_in"
        if user.get("status") != "approved":
            return False, f"status_{user.get('status')}"
        return True, "ok"

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always-open paths
        if path in _ALWAYS_OPEN or path.startswith(_ALWAYS_OPEN_PREFIXES):
            return await call_next(request)

        effective_keys = self._effective_keys(request)

        # -----------------------------------------------------------------
        # Bearer-only paths (/v1/*) — OpenAI-compatible API
        # -----------------------------------------------------------------
        if path.startswith(_BEARER_ONLY_PREFIXES):
            if not effective_keys:
                if self._require_auth:
                    # Strict / fail-closed: reject all calls until the admin
                    # configures at least one gateway token.
                    return _error_401(
                        "Gateway is not configured: no GATEWAY_API_KEYS and "
                        "no dynamic tokens. Refusing to make outbound LLM "
                        "calls. Create a token at /settings → Gateway Keys."
                    )
                # Legacy permissive mode (REQUIRE_AUTH=false)
                return await call_next(request)
            if self._check_bearer(request, effective_keys):
                return await call_next(request)
            return _error_401("Missing or invalid Bearer token")

        # -----------------------------------------------------------------
        # Everything else — UI + admin APIs
        # -----------------------------------------------------------------
        if self._sso_enabled:
            ok, reason = self._check_session(request)
            if ok:
                return await call_next(request)

            # Also accept a valid gateway Bearer token for admin APIs so
            # automated tooling (curl / scripts) can still hit /api/* with
            # the gateway key even when SSO is on. This does NOT apply to
            # HTML page routes.
            if self._check_bearer(request, effective_keys) and effective_keys:
                return await call_next(request)

            if _wants_json(request):
                if reason == "status_pending":
                    return _error_403("Account is awaiting admin approval")
                if reason == "status_rejected":
                    return _error_403("Account access has been revoked")
                return _error_401("Authentication required")
            # HTML — redirect to login
            return RedirectResponse(url="/login", status_code=302)

        # -----------------------------------------------------------------
        # SSO disabled — legacy Bearer-only protection for UI too
        # -----------------------------------------------------------------
        if not effective_keys:
            return await call_next(request)  # no keys configured → open
        if self._check_bearer(request, effective_keys):
            return await call_next(request)
        if _wants_json(request):
            return _error_401("Missing or invalid Bearer token")
        # Page request without a cookie — let the UI request /api endpoints
        # which will 401 and trigger the client-side login redirect. For
        # direct page GETs send a simple 401 page.
        return _error_401("Authentication required")


# ---------------------------------------------------------------------------
# Cloudflare Access JWT middleware (unchanged)
# ---------------------------------------------------------------------------


class _JWKSCache:
    _TTL = 3600

    def __init__(self):
        self._keys: Optional[List[dict]] = None
        self._fetched_at: float = 0.0

    def is_fresh(self) -> bool:
        return self._keys is not None and (time.time() - self._fetched_at) < self._TTL

    def store(self, keys: List[dict]) -> None:
        self._keys = keys
        self._fetched_at = time.time()

    def get(self) -> Optional[List[dict]]:
        return self._keys if self.is_fresh() else None


_jwks_cache = _JWKSCache()


async def _fetch_cf_public_keys(team_name: str) -> List[dict]:
    cached = _jwks_cache.get()
    if cached is not None:
        return cached

    url = f"https://{team_name}.cloudflareaccess.com/cdn-cgi/access/certs"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    data = resp.json()
    keys = data.get("keys", [])
    _jwks_cache.store(keys)
    logger.info("CloudflareAccess: fetched %d public key(s)", len(keys))
    return keys


def _b64url_decode(s: str) -> bytes:
    import base64
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT")
    return json.loads(_b64url_decode(parts[1]))


def _verify_jwt_rs256(token: str, jwk: dict) -> dict:
    try:
        import jwt as pyjwt  # PyJWT
        from jwt.algorithms import RSAAlgorithm
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        return pyjwt.decode(
            token, public_key, algorithms=["RS256"],
            options={"verify_exp": True},
        )
    except ImportError:
        logger.warning(
            "PyJWT not installed — JWT signature not verified. "
            "Install PyJWT[crypto] for full validation."
        )
        return _decode_jwt_payload(token)
    except Exception as exc:
        raise ValueError(f"JWT verification failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Feature 2: Google OAuth2 session middleware (web UI pages only)
# ---------------------------------------------------------------------------

# Web UI paths that require a valid session when Google OAuth is configured
_WEB_UI_PATHS: frozenset = frozenset([
    "/dashboard", "/analytics", "/settings", "/playground",
    "/logs", "/images", "/api-docs",
])


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """
    Validate Cloudflare Access JWT in the ``Cf-Access-Jwt-Assertion`` header.
    Activated only when ``ENABLE_CF_ACCESS=True``.
    """

    def __init__(self, app, team_name: str, aud: str):
        super().__init__(app)
        self._team_name = team_name
        self._aud = aud

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if (path in _ALWAYS_OPEN
                or path.startswith(_ALWAYS_OPEN_PREFIXES)):
            return await call_next(request)

        jwt_token = request.headers.get("Cf-Access-Jwt-Assertion", "")
        if not jwt_token:
            return _error_403("Missing Cf-Access-Jwt-Assertion header")

        try:
            public_keys = await _fetch_cf_public_keys(self._team_name)
        except Exception as exc:
            logger.error("Failed to fetch Cloudflare Access public keys: %s", exc)
            return _error_403("Cannot validate Cloudflare Access token")

        verified_payload: Optional[dict] = None
        for jwk in public_keys:
            try:
                verified_payload = _verify_jwt_rs256(jwt_token, jwk)
                break
            except Exception:
                continue

        if verified_payload is None:
            return _error_403("Invalid Cloudflare Access JWT")

        expected_iss = f"https://{self._team_name}.cloudflareaccess.com"
        iss = verified_payload.get("iss", "")
        aud = verified_payload.get("aud", [])
        if isinstance(aud, str):
            aud = [aud]

        if iss != expected_iss:
            return _error_403("JWT issuer mismatch")
        if self._aud and self._aud not in aud:
            return _error_403("JWT audience mismatch")

        request.state.cf_identity = verified_payload
        return await call_next(request)


# ---------------------------------------------------------------------------
# Bearer-token redaction filter for logs
# ---------------------------------------------------------------------------

class BearerRedactFilter(logging.Filter):
    """
    Scrub Bearer tokens and obvious API-key shapes from log records so a
    misconfigured ``DEBUG`` log line can't leak credentials.
    """

    _BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9\._\-/+=]{8,}")
    _OBVIOUS_KEY = re.compile(
        r"\b(sk-[A-Za-z0-9]{10,}|gsk_[A-Za-z0-9]{10,}|csk-[A-Za-z0-9]{10,}|"
        r"hf_[A-Za-z0-9]{10,}|AIza[A-Za-z0-9_-]{20,})"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = self._BEARER.sub("Bearer [REDACTED]", msg)
        redacted = self._OBVIOUS_KEY.sub("[REDACTED-KEY]", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True
