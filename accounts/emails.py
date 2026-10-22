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

from payments.email import _button, _h, _p, _shell

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


def send_verification_email(user, verify_url: str) -> None:
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send verification email to %s.", user.email)
        return
    resend.api_key = settings.RESEND_API_KEY
    name = html.escape(user.first_name or "there")
    body = (_h(f"Confirm your email, {name}")
            + _p("Thanks for creating an Azura Travels account. Please confirm this email address to finish setting up your account.")
            + _button(verify_url, "Verify my email")
            + _p("This link expires in 7 days. If you didn't create this account, you can ignore this email.", small=True))
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
    body = (_h(f"Akwaaba, {name}")
            + _p("Your email is verified and your Azura Travels account is ready. Your bookings, payments and travel "
                 "information are all in your dashboard.")
            + _button(dashboard_url, "Go to my dashboard"))
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
