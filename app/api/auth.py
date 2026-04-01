"""
Google OAuth2 login for the Arbiter web UI.

Endpoints
─────────
GET /login               → serve login.html
GET /auth/login          → redirect to Google OAuth consent page
GET /auth/callback       → exchange code for token, set session cookie
GET /auth/logout         → clear session cookie, redirect to /login
GET /auth/me             → return current user info (JSON)

Sessions are stored as signed JWT cookies (HttpOnly, SameSite=Lax).
When GOOGLE_CLIENT_ID is not configured, /auth/me returns {"enabled": false}
so the UI knows auth is disabled.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt as jose_jwt

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Auth"])

# ── Session JWT config ────────────────────────────────────────────────────────
_SESSION_COOKIE = "arbiter_session"
_SESSION_TTL    = 86400  # 24 hours

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "static",
)

# Use configured secret or generate a random one per process startup.
# Production should always set SESSION_SECRET in .env so tokens survive restart.
_SECRET = settings.SESSION_SECRET or secrets.token_hex(32)

# Google OAuth2 endpoints
_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v3/userinfo"

# In-memory state nonce set (prevents CSRF; per-process, sufficient for single instance)
_pending_states: set[str] = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _allowed(email: str) -> bool:
    """Return True if the authenticated email is permitted to access the UI."""
    if not settings.GOOGLE_ALLOWED_EMAILS and not settings.GOOGLE_ALLOWED_DOMAINS:
        return True  # no restriction — all authenticated Google users allowed

    allowed_emails = {e.strip().lower() for e in settings.GOOGLE_ALLOWED_EMAILS.split(",") if e.strip()}
    allowed_domains = {d.strip().lower() for d in settings.GOOGLE_ALLOWED_DOMAINS.split(",") if d.strip()}

    email_lower = email.lower()
    if email_lower in allowed_emails:
        return True
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    return domain in allowed_domains


def _make_session_token(user: dict) -> str:
    payload = {
        "sub":     user["email"],
        "email":   user["email"],
        "name":    user.get("name", ""),
        "picture": user.get("picture", ""),
        "iat":     int(time.time()),
        "exp":     int(time.time()) + _SESSION_TTL,
    }
    return jose_jwt.encode(payload, _SECRET, algorithm="HS256")


def _decode_session_token(token: str) -> Optional[dict]:
    try:
        return jose_jwt.decode(token, _SECRET, algorithms=["HS256"])
    except Exception:
        return None


def _get_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return None
    return _decode_session_token(token)


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS proxy in production
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(_SESSION_COOKIE)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page() -> HTMLResponse:
    path = os.path.join(_STATIC_DIR, "login.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse("<h1>Login page not found</h1>", status_code=404)


@router.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request):
    """Redirect the browser to Google's OAuth consent page."""
    if not _is_enabled():
        return RedirectResponse("/dashboard")

    state = secrets.token_urlsafe(32)
    _pending_states.add(state)

    # Store state in session so it survives the redirect
    params = {
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    }
    from urllib.parse import urlencode
    url = f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url, status_code=302)


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Google OAuth2 callback."""
    if error:
        return RedirectResponse(f"/login?error={error}", status_code=302)

    if not code:
        return RedirectResponse("/login?error=no_code", status_code=302)

    # Validate state (CSRF protection)
    if state not in _pending_states:
        return RedirectResponse("/login?error=invalid_state", status_code=302)
    _pending_states.discard(state)

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code":          code,
                    "client_id":     settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
                    "grant_type":    "authorization_code",
                },
            )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.warning("No access_token in Google response: %s", token_data)
            return RedirectResponse("/login?error=token_exchange_failed", status_code=302)

        # Fetch user info
        async with httpx.AsyncClient(timeout=10.0) as client:
            user_resp = await client.get(
                _GOOGLE_USERINFO,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        user = user_resp.json()
        email = user.get("email", "")

    except Exception as exc:
        logger.error("OAuth callback error: %s", exc)
        return RedirectResponse("/login?error=server_error", status_code=302)

    if not email:
        return RedirectResponse("/login?error=no_email", status_code=302)

    if not _allowed(email):
        logger.warning("Google login rejected for %s (not in allowlist)", email)
        return RedirectResponse("/login?error=not_allowed", status_code=302)

    # Create session
    session_token = _make_session_token(user)
    response = RedirectResponse("/dashboard", status_code=302)
    _set_session_cookie(response, session_token)
    logger.info("Google login: %s (%s)", user.get("name"), email)
    return response


@router.get("/auth/logout", include_in_schema=False)
async def auth_logout():
    """Clear the session cookie and redirect to login."""
    response = RedirectResponse("/login", status_code=302)
    _clear_session_cookie(response)
    return response


@router.get("/auth/me", summary="Current user info")
async def auth_me(request: Request) -> JSONResponse:
    """Return the authenticated user's info, or 401 if not signed in."""
    if not _is_enabled():
        return JSONResponse({"enabled": False, "authenticated": True})

    session = _get_session(request)
    if not session:
        return JSONResponse({"enabled": True, "authenticated": False}, status_code=401)

    return JSONResponse({
        "enabled":       True,
        "authenticated": True,
        "email":         session.get("email"),
        "name":          session.get("name"),
        "picture":       session.get("picture"),
    })
