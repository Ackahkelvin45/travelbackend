"""
Account lifecycle emails: verification (sent at signup) and welcome (sent once
the account is verified). Same conventions as payments/email.py — Resend SDK,
log-only when RESEND_API_KEY is unset, every failure swallowed + logged so an
email problem can never break signup, and all user-controlled text escaped.
"""
import html
import logging

import resend
from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

_VERIFY_SALT = "email-verify"
_VERIFY_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def make_verification_token(user) -> str:
    return signing.dumps({"user": str(user.id)}, salt=_VERIFY_SALT)


def read_verification_token(token: str):
    """Return the user id encoded in a token, or None if invalid/expired."""
    try:
        data = signing.loads(token, salt=_VERIFY_SALT, max_age=_VERIFY_MAX_AGE)
        return data.get("user")
    except signing.BadSignature:
        return None


def _shell(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;">{body_html}</td></tr>
  <tr><td style="padding:16px 40px;background:#faf8f5;color:#7a7a88;font-size:11px;">
    &copy; Azura Travels &middot; azuratravels.live
  </td></tr>
</table></td></tr></table></body></html>"""


def send_verification_email(user, verify_url: str) -> None:
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send verification email to %s.", user.email)
        return
    resend.api_key = settings.RESEND_API_KEY
    name = html.escape(user.first_name or "there")
    body = f"""
      <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Confirm your email, {name}</p>
      <p style="margin:0 0 20px;color:#555;font-size:14px;line-height:1.6;">
        Thanks for creating an Azura Travels account. Please confirm this email
        address to finish setting up your account.
      </p>
      <a href="{html.escape(verify_url)}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Verify my email</a>
      <p style="margin:22px 0 0;color:#999;font-size:12px;line-height:1.6;">
        This link expires in 7 days. If you didn't create this account, you can ignore this email.
      </p>"""
    try:
        email = resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user.email],
            "subject": "Verify your email – Azura Travels",
            "html": _shell("Verify your email", body),
        })
        logger.info("Verification email sent id=%s to=%s", email.get("id"), user.email)
    except Exception as exc:
        logger.exception("Failed to send verification email to %s: %s", user.email, str(exc))


def send_welcome_email(user) -> None:
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send welcome email to %s.", user.email)
        return
    resend.api_key = settings.RESEND_API_KEY
    name = html.escape(user.first_name or "there")
    dashboard_url = f"{settings.FRONTEND_URL}/dashboard"
    body = f"""
      <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Welcome aboard, {name}! &#127881;</p>
      <p style="margin:0 0 20px;color:#555;font-size:14px;line-height:1.6;">
        Your email is verified and your Azura Travels account is ready. Browse our
        curated tours, book with installments, and track everything from your dashboard.
      </p>
      <a href="{html.escape(dashboard_url)}" style="display:inline-block;padding:14px 24px;background:#1a1a2e;color:#d4a843;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Go to your dashboard</a>
      <p style="margin:22px 0 0;color:#999;font-size:12px;line-height:1.6;">
        Questions? Just reply to this email — we're happy to help.
      </p>"""
    try:
        email = resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user.email],
            "subject": "Welcome to Azura Travels",
            "html": _shell("Welcome to Azura Travels", body),
        })
        logger.info("Welcome email sent id=%s to=%s", email.get("id"), user.email)
    except Exception as exc:
        logger.exception("Failed to send welcome email to %s: %s", user.email, str(exc))
