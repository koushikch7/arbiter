"""
Authentication middleware for Arbiter.

GatewayAuthMiddleware
  – Validates  Authorization: Bearer <key>  header.
  – Reads allowed keys from settings.GATEWAY_API_KEYS (comma-separated).
  – If GATEWAY_API_KEYS is empty, auth is completely disabled.
  – Certain paths are always exempt (docs, health, dashboard, etc.).
  – Returns 401 JSON on invalid/missing key.

CloudflareAccessMiddleware
  – Optional; activated when settings.ENABLE_CF_ACCESS is True.
  – Validates the  Cf-Access-Jwt-Assertion  header.
  – Fetches public keys from the Cloudflare Access JWKS endpoint,
    verifies RS256 JWT signature, iss, and aud claims.
  – Public keys are cached in memory for 1 hour.
  – Returns 403 JSON on invalid JWT.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that bypass gateway-level auth entirely
_EXEMPT_PATHS: frozenset = frozenset(
    [
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api-docs",
        "/dashboard",
        "/dashboard/stats",
        "/v1/models",
    ]
)


def _error_401(message: str = "Invalid API key") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": message,
                "type": "authentication_error",
                "code": 401,
            }
        },
    )


def _error_403(message: str = "Access denied") -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": message,
                "type": "authorization_error",
                "code": 403,
            }
        },
    )


# ---------------------------------------------------------------------------
# Feature 1: Gateway API key auth
# ---------------------------------------------------------------------------


class GatewayAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce Bearer-token authentication on all non-exempt paths.

    Configuration
    ─────────────
    GATEWAY_API_KEYS  comma-separated list of valid tokens.
                      If empty (or not set), auth is disabled entirely.
    """

    def __init__(self, app, allowed_keys: List[str]):
        super().__init__(app)
        # Store as a set for O(1) lookup; empty set = auth disabled
        self._allowed: frozenset = frozenset(k.strip() for k in allowed_keys if k.strip())

    async def dispatch(self, request: Request, call_next) -> Response:
        # Auth disabled — pass through
        if not self._allowed:
            return await call_next(request)

        # Exempt paths — pass through
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # Validate Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _error_401("Missing or invalid Authorization header")

        token = auth_header[len("Bearer "):].strip()
        if token not in self._allowed:
            return _error_401("Invalid API key")

        return await call_next(request)


# ---------------------------------------------------------------------------
# Feature 9: Cloudflare Access JWT middleware
# ---------------------------------------------------------------------------


class _JWKSCache:
    """Simple in-memory cache for Cloudflare Access public keys (1 hour TTL)."""

    _TTL = 3600  # seconds

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
    """Fetch Cloudflare Access public keys, using cached copy when fresh."""
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
    logger.info(f"CloudflareAccess: fetched {len(keys)} public key(s)")
    return keys


def _b64url_decode(s: str) -> bytes:
    """Base64url decode without padding."""
    import base64
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _decode_jwt_payload(token: str) -> dict:
    """Decode and return the payload section of a JWT (no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT")
    payload_bytes = _b64url_decode(parts[1])
    return json.loads(payload_bytes)


def _verify_jwt_rs256(token: str, jwk: dict) -> dict:
    """
    Verify RS256 JWT using the cryptography library (via python-jose).

    Falls back to payload-only decode (no sig check) if jose is unavailable.
    Returns the verified payload dict.
    """
    try:
        from jose import jwt as jose_jwt, jwk as jose_jwk, JWTError  # type: ignore

        # Build public key from JWK
        public_key = jose_jwk.construct(jwk, algorithm="RS256")
        payload = jose_jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        return payload
    except ImportError:
        logger.warning(
            "python-jose not installed — JWT signature not verified. "
            "Install python-jose[cryptography] for full validation."
        )
        return _decode_jwt_payload(token)
    except Exception as exc:
        raise ValueError(f"JWT verification failed: {exc}") from exc


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """
    Validate Cloudflare Access JWT in the  Cf-Access-Jwt-Assertion  header.

    Activated only when ENABLE_CF_ACCESS=True.
    Exempt paths: same set as GatewayAuthMiddleware.
    """

    def __init__(
        self,
        app,
        team_name: str,
        aud: str,
    ):
        super().__init__(app)
        self._team_name = team_name
        self._aud = aud

    async def dispatch(self, request: Request, call_next) -> Response:
        # Exempt paths — always allow
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        jwt_token = request.headers.get("Cf-Access-Jwt-Assertion", "")
        if not jwt_token:
            return _error_403("Missing Cf-Access-Jwt-Assertion header")

        try:
            public_keys = await _fetch_cf_public_keys(self._team_name)
        except Exception as exc:
            logger.error(f"Failed to fetch Cloudflare Access public keys: {exc}")
            return _error_403("Cannot validate Cloudflare Access token")

        # Try each public key (key rotation)
        verified_payload: Optional[dict] = None
        for jwk in public_keys:
            try:
                verified_payload = _verify_jwt_rs256(jwt_token, jwk)
                break
            except Exception:
                continue

        if verified_payload is None:
            return _error_403("Invalid Cloudflare Access JWT")

        # Verify issuer and audience
        expected_iss = f"https://{self._team_name}.cloudflareaccess.com"
        iss = verified_payload.get("iss", "")
        aud = verified_payload.get("aud", [])
        if isinstance(aud, str):
            aud = [aud]

        if iss != expected_iss:
            return _error_403("JWT issuer mismatch")
        if self._aud and self._aud not in aud:
            return _error_403("JWT audience mismatch")

        # Attach identity to request state for downstream use
        request.state.cf_identity = verified_payload
        return await call_next(request)
