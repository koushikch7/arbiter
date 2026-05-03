"""
SMTP Email Service for Arbiter.

Handles sending emails via configured SMTP relay (Zoho, Gmail, etc.).
Used by: daily analytics reports, user invitations, error alerts.

Configuration is read from environment via app.config.settings.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    """Async-safe SMTP email sender using a thread pool."""

    def __init__(self):
        self.host = settings.SMTP_HOST
        self.port = settings.SMTP_PORT
        self.username = settings.SMTP_USERNAME
        self.password = settings.SMTP_PASSWORD
        self.from_email = settings.SMTP_FROM
        self.from_name = settings.SMTP_FROM_NAME
        self.to_email = settings.SMTP_TO

    @property
    def configured(self) -> bool:
        """Return True if SMTP is properly configured."""
        return bool(self.host and self.username and self.password)

    def _send_sync(
        self,
        to: str,
        subject: str,
        html_body: str,
        plain_body: Optional[str] = None,
    ) -> bool:
        """Synchronous send — runs in executor thread."""
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{self.from_name} <{self.from_email}>"
        msg["To"] = to
        msg["Subject"] = subject

        if plain_body:
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.username, self.password)
                server.sendmail(self.from_email, [to], msg.as_string())
            logger.info("Email sent to %s: %s", to, subject)
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error("SMTP auth failed: %s", e)
            return False
        except smtplib.SMTPException as e:
            logger.error("SMTP error sending to %s: %s", to, e)
            return False
        except Exception as e:
            logger.error("Unexpected email error: %s", e)
            return False

    async def send(
        self,
        to: str,
        subject: str,
        html_body: str,
        plain_body: Optional[str] = None,
    ) -> bool:
        """Send an email asynchronously (non-blocking)."""
        if not self.configured:
            logger.warning("Email not sent — SMTP not configured")
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._send_sync, to, subject, html_body, plain_body
        )

    async def send_to_admin(
        self,
        subject: str,
        html_body: str,
        plain_body: Optional[str] = None,
    ) -> bool:
        """Send email to the configured admin address."""
        if not self.to_email:
            logger.warning("No SMTP_TO configured — skipping admin email")
            return False
        return await self.send(self.to_email, subject, html_body, plain_body)

    async def send_invite(
        self,
        to_email: str,
        invite_url: str,
        inviter_name: str = "Arbiter Admin",
    ) -> bool:
        """Send a user invitation email."""
        subject = "You've been invited to Arbiter"
        html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333">
  <div style="text-align:center;margin-bottom:30px">
    <div style="display:inline-block;width:48px;height:48px;background:#6366f1;border-radius:12px;line-height:48px;color:#fff;font-size:24px;font-weight:700">A</div>
    <h1 style="margin:12px 0 4px;font-size:22px;color:#111">Arbiter Gateway</h1>
  </div>
  <div style="background:#f8f9fa;border-radius:8px;padding:24px;border:1px solid #e5e7eb">
    <p style="margin:0 0 16px;font-size:15px"><strong>{inviter_name}</strong> has invited you to access the Arbiter LLM Gateway.</p>
    <p style="margin:0 0 20px;font-size:14px;color:#555">Click the button below to accept your invitation and sign in with your Google account:</p>
    <div style="text-align:center;margin:24px 0">
      <a href="{invite_url}" style="display:inline-block;padding:12px 32px;background:#6366f1;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px">Accept Invitation</a>
    </div>
    <p style="margin:0;font-size:12px;color:#888">If the button doesn't work, copy this link:<br><a href="{invite_url}" style="color:#6366f1">{invite_url}</a></p>
  </div>
  <p style="text-align:center;margin-top:20px;font-size:11px;color:#999">Arbiter — Intelligent LLM Router &amp; Gateway</p>
</body>
</html>"""
        plain = f"{inviter_name} has invited you to Arbiter.\n\nAccept: {invite_url}"
        return await self.send(to_email, subject, html, plain)


# Singleton instance
email_service = EmailService()
