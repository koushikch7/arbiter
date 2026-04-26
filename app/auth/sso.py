"""
Google SSO integration with admin-approval workflow.

Flow
----
1. Unauthenticated user hits `/auth/login` → redirected to Google's consent screen.
2. Google calls back `/auth/callback?code=…&state=…` — we verify state
   (CSRF), exchange code for an ID token, and extract email / name / picture.
3. If email == ``ADMIN_EMAIL``, the user is auto-bootstrapped as an approved
   admin. Otherwise the user record is created in ``pending`` status.
4. Session cookie is set via Starlette ``SessionMiddleware`` (HMAC-signed,
   NOT stored server-side). Only **approved** users can access protected
   pages; pending users see the ``/auth/pending`` screen; rejected users
   hit a 403.
5. Session revocation: when an admin rejects a user, ``session_version``
   in the user record is bumped. The middleware compares the session's
   ``version`` claim and logs out any mismatched session on the next
   request.

Security
--------
  * Session cookie is ``HttpOnly``, ``SameSite=Lax``, ``Secure`` in prod
    (driven by ``SESSION_COOKIE_SECURE`` env).
  * State parameter validated on callback (Authlib handles this).
  * ID token signature + audience verified by Authlib's ``parse_id_token``.
  * Admin email comparison is case-insensitive.
  * Session max-age: 24 hours.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.state_store import (
    get_user,
    upsert_user,
    record_user_login,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])

# ---------------------------------------------------------------------------
# OAuth client setup
# ---------------------------------------------------------------------------

oauth = OAuth()


def register_google_oauth() -> None:
    """Register the Google OAuth client. Safe to call multiple times."""
    if not (settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET):
        logger.info("Google SSO not configured (GOOGLE_OAUTH_CLIENT_ID missing)")
        return
    if "google" in oauth._clients:
        return  # already registered

    oauth.register(
        name="google",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    logger.info("Google SSO registered (client_id=%s...)",
                settings.GOOGLE_OAUTH_CLIENT_ID[:8])


def sso_enabled() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _set_user_session(request: Request, user: dict) -> None:
    """Write user identity into the signed session cookie."""
    request.session["user"] = {
        "email":    user["email"],
        "name":     user.get("name", ""),
        "picture":  user.get("picture", ""),
        "is_admin": bool(user.get("is_admin")),
        "status":   user.get("status", "pending"),
        "version":  int(user.get("session_version", 1)),
    }


def get_session_user(request: Request) -> dict | None:
    """
    Return the logged-in user from the session, or None if not logged in /
    session is invalidated.
    """
    # Defensive: SessionMiddleware might not be in the ASGI scope yet (e.g.
    # when GatewayAuthMiddleware runs outside of it due to middleware-stack
    # ordering, or when SSO is disabled entirely). Treat as "no session".
    if "session" not in request.scope:
        return None
    data = request.session.get("user")
    if not data:
        return None
    # Validate against state store (catches revoked sessions)
    stored = get_user(data.get("email", ""))
    if stored is None:
        return None
    if int(stored.get("session_version", 1)) != int(data.get("version", 0)):
        # Session was invalidated (admin rejected / bumped version)
        return None
    # Refresh status / admin from authoritative source
    data["status"]   = stored.get("status", "pending")
    data["is_admin"] = bool(stored.get("is_admin"))
    return data


def clear_session(request: Request) -> None:
    request.session.clear()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/config", summary="Get SSO configuration (public)")
async def auth_config() -> JSONResponse:
    """Expose whether SSO is configured (for the login page)."""
    return JSONResponse({
        "sso_enabled": sso_enabled(),
        "admin_email": settings.ADMIN_EMAIL or None,
    })


@router.get("/me", summary="Get the current logged-in user")
async def auth_me(request: Request) -> JSONResponse:
    user = get_session_user(request)
    if user is None:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({"authenticated": True, **user})


@router.get("/login", summary="Initiate Google SSO login")
async def login(request: Request):
    if not sso_enabled():
        # SSO not configured — redirect to dashboard (auth middleware will
        # let them through when no credentials are set)
        return RedirectResponse("/dashboard")

    # Compute redirect URI — prefer APP_BASE_URL when set (useful behind
    # reverse proxies / CF tunnel where request.url may be http://).
    base = (settings.APP_BASE_URL or str(request.base_url).rstrip("/")).rstrip("/")
    redirect_uri = f"{base}/auth/callback"
    client = oauth.create_client("google")
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/callback", summary="Google OAuth callback")
async def callback(request: Request):
    if not sso_enabled():
        raise HTTPException(400, "SSO is not configured")

    client = oauth.create_client("google")
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OAuth callback failed: %s", exc)
        return RedirectResponse("/login?error=oauth_failed", status_code=302)

    userinfo: dict[str, Any] = token.get("userinfo") or {}
    if not userinfo:
        # Fall back to calling the userinfo endpoint explicitly
        try:
            resp = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo", token=token
            )
            userinfo = resp.json()
        except Exception as exc:
            logger.error("Failed to fetch Google userinfo: %s", exc)
            return RedirectResponse("/login?error=userinfo_failed", status_code=302)

    email = (userinfo.get("email") or "").lower().strip()
    if not email:
        return RedirectResponse("/login?error=no_email", status_code=302)

    # Some enterprises want only verified emails
    if userinfo.get("email_verified") is False:
        return RedirectResponse("/login?error=email_unverified", status_code=302)

    admin_email = (settings.ADMIN_EMAIL or "").lower().strip()
    is_admin = bool(admin_email) and email == admin_email

    existing = get_user(email)
    if existing is None:
        # First login — bootstrap
        user = upsert_user(
            email=email,
            name=userinfo.get("name", ""),
            picture=userinfo.get("picture", ""),
            status="approved" if is_admin else "pending",
            is_admin=is_admin,
        )
        logger.info(
            "New user %s (admin=%s, status=%s)", email, is_admin, user["status"]
        )
    else:
        # Keep existing status unless it's the admin email (idempotent admin bootstrap)
        updates: dict[str, Any] = {
            "name":    userinfo.get("name", existing.get("name", "")),
            "picture": userinfo.get("picture", existing.get("picture", "")),
        }
        if is_admin and not existing.get("is_admin"):
            updates["is_admin"] = True
            updates["status"]   = "approved"
        user = upsert_user(email=email, **updates)

    record_user_login(email)

    # Pending / rejected — don't set session, redirect to informational page
    if user.get("status") != "approved":
        return RedirectResponse(
            f"/auth/pending?email={email}&status={user.get('status')}", status_code=302
        )

    _set_user_session(request, user)
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/logout", summary="Log out the current user")
@router.get("/logout", summary="Log out (GET for link-based logout)")
async def logout(request: Request):
    clear_session(request)
    return RedirectResponse("/login?logout=1", status_code=302)


@router.get("/pending", summary="Pending / rejected-user landing page")
async def pending(request: Request) -> HTMLResponse:
    email = request.query_params.get("email", "")
    status = request.query_params.get("status", "pending")
    admin = settings.ADMIN_EMAIL or "the administrator"

    if status == "rejected":
        title = "Access Denied"
        body = (
            f"Your account <strong>{email}</strong> has been denied access to this "
            f"Arbiter instance. Please contact {admin} if you believe this is an error."
        )
    else:
        title = "Access Pending"
        body = (
            f"Your account <strong>{email}</strong> has been registered and is "
            f"awaiting admin approval. Please contact {admin} for approval, "
            f"then return to sign in again."
        )

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title} — Arbiter</title>
<style>
body{{font-family:system-ui;background:#0b0d10;color:#eaeaea;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}}
.card{{background:#14171c;border:1px solid #2a2f36;border-radius:12px;padding:40px;
max-width:480px;text-align:center}}
h1{{margin:0 0 16px;font-size:24px}}
p{{line-height:1.6;color:#bbb}}
a{{color:#5eb3ff;text-decoration:none;margin-top:24px;display:inline-block}}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{body}</p>
<a href="/auth/logout">Sign out</a></div></body></html>"""
    )
