"""
Admin-only user management API.

Only the admin (identified by ``ADMIN_EMAIL`` / ``is_admin`` flag) can list
users, approve pending users, reject / re-activate users, or delete user
records. Regular approved users get a 403.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.state_store import (
    list_users,
    set_user_status,
    delete_user,
    upsert_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["Users"])


# ---------------------------------------------------------------------------
# Admin dependency
# ---------------------------------------------------------------------------


def require_admin(request: Request) -> dict:
    """
    FastAPI dependency — raises 401 if unauthenticated, 403 if not admin.

    Accepts either:
      * A logged-in admin SSO session (browser/UI), OR
      * A valid Bearer gateway API token (automation/tooling).

    Can be used directly on endpoints even outside this module::

        from app.auth.users import require_admin
        @app.get("/my/admin/route", dependencies=[Depends(require_admin)])
        async def my_route(): ...
    """
    # Import here to avoid circular: sso → state_store → users_api
    from app.auth.sso import get_session_user, sso_enabled

    if not sso_enabled():
        # SSO disabled — admin protection falls back to bearer-gateway auth,
        # which is already enforced by GatewayAuthMiddleware for these paths.
        # We can't identify a specific user, so allow through.
        return {"email": "(sso-disabled)", "is_admin": True}

    # 1. Try SSO session
    user = get_session_user(request)
    if user is not None:
        if not user.get("is_admin"):
            raise HTTPException(403, "Admin access required")
        return user

    # 2. Fall back to Bearer gateway token — automation/tooling path.
    #    The middleware already validated the token; we just need to
    #    confirm a valid bearer was presented.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(None, 1)[1].strip()
        if token:
            allowed = getattr(request.app.state, "gateway_tokens", set()) or set()
            if token in allowed:
                return {"email": "(bearer-token)", "is_admin": True, "via": "bearer"}

    raise HTTPException(401, "Not authenticated")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", summary="List all users")
async def list_all(admin: dict = Depends(require_admin)) -> JSONResponse:
    users = list_users()
    # Sort: pending first, then approved, then rejected
    order = {"pending": 0, "approved": 1, "rejected": 2}
    users.sort(key=lambda u: (order.get(u.get("status", "pending"), 99),
                              u.get("email", "")))
    return JSONResponse({
        "admin_email": settings.ADMIN_EMAIL or None,
        "users":       users,
    })


class StatusBody(BaseModel):
    status: str  # "approved" | "pending" | "rejected"


@router.post("/{email}/status", summary="Change a user's status")
async def change_status(
    email: str, body: StatusBody, admin: dict = Depends(require_admin)
) -> JSONResponse:
    if body.status not in ("approved", "pending", "rejected"):
        raise HTTPException(422, "invalid status")

    # Protect admin from self-lockout
    if email.lower() == admin.get("email", "").lower() and body.status != "approved":
        raise HTTPException(400, "cannot change your own admin status")

    updated = set_user_status(email, body.status)
    if updated is None:
        raise HTTPException(404, f"user {email!r} not found")

    logger.info(
        "User %s set to %s by admin %s", email, body.status, admin.get("email")
    )
    return JSONResponse(updated)


@router.post("/{email}/approve", summary="Approve a pending user")
async def approve(email: str, admin: dict = Depends(require_admin)) -> JSONResponse:
    updated = set_user_status(email, "approved")
    if updated is None:
        raise HTTPException(404, f"user {email!r} not found")
    logger.info("User %s approved by admin %s", email, admin.get("email"))
    return JSONResponse(updated)


@router.post("/{email}/reject", summary="Reject / revoke a user")
async def reject(email: str, admin: dict = Depends(require_admin)) -> JSONResponse:
    if email.lower() == admin.get("email", "").lower():
        raise HTTPException(400, "cannot reject yourself")
    updated = set_user_status(email, "rejected")
    if updated is None:
        raise HTTPException(404, f"user {email!r} not found")
    logger.info("User %s rejected by admin %s", email, admin.get("email"))
    return JSONResponse(updated)


@router.delete("/{email}", summary="Delete a user record")
async def remove(email: str, admin: dict = Depends(require_admin)) -> JSONResponse:
    if email.lower() == admin.get("email", "").lower():
        raise HTTPException(400, "cannot delete yourself")
    ok = delete_user(email)
    if not ok:
        raise HTTPException(404, f"user {email!r} not found")
    logger.info("User %s deleted by admin %s", email, admin.get("email"))
    return JSONResponse({"deleted": email})


class CreateUserBody(BaseModel):
    email: str
    name: str = ""
    status: str = "approved"
    is_admin: bool = False


@router.post("", summary="Pre-approve a user (whitelist an email)", status_code=201)
async def create(
    body: CreateUserBody, admin: dict = Depends(require_admin)
) -> JSONResponse:
    """
    Pre-approve a user so they can log in straight away on first Google SSO.
    Useful for seeding teammates.
    """
    if body.status not in ("approved", "pending", "rejected"):
        raise HTTPException(422, "invalid status")
    user = upsert_user(
        email=body.email, name=body.name,
        status=body.status, is_admin=body.is_admin,
    )
    logger.info(
        "User %s pre-approved (admin=%s) by %s",
        body.email, body.is_admin, admin.get("email")
    )
    return JSONResponse(user, status_code=201)


class InviteBody(BaseModel):
    email: str
    is_admin: bool = False


@router.post("/invite", summary="Invite a user via email", status_code=201)
async def invite_user(
    body: InviteBody, admin: dict = Depends(require_admin)
) -> JSONResponse:
    """
    Send an invitation email to a user. The user is pre-approved immediately
    so they can sign in as soon as they click the link. The email contains a
    direct link to the app's login page.
    """
    from app.services.email_service import email_service

    if not email_service.configured:
        raise HTTPException(
            503,
            "SMTP is not configured. Add SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD to .env"
        )

    # Pre-approve the user
    user = upsert_user(
        email=body.email,
        name="",
        status="approved",
        is_admin=body.is_admin,
    )

    # Send the invitation email
    invite_url = f"{settings.APP_BASE_URL}/auth/login"
    inviter = admin.get("email", "Admin")
    sent = await email_service.send_invite(
        to_email=body.email,
        invite_url=invite_url,
        inviter_name=inviter,
    )

    if not sent:
        logger.warning("Invite email failed to send to %s (user still pre-approved)", body.email)
        return JSONResponse(
            {"detail": "User pre-approved but email delivery failed. Check SMTP config.", "user": user},
            status_code=201,
        )

    logger.info("Invitation sent to %s by %s", body.email, inviter)
    return JSONResponse(
        {"detail": f"Invitation sent to {body.email}", "user": user},
        status_code=201,
    )
