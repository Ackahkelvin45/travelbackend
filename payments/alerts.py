"""
payments/alerts.py
------------------
Critical money events must reach a human, not just a log file. Used for:
gateway amount mismatches, overpayments, payments landing on cancelled
bookings, FX feed rejections and FX outages.

Fail-safe by design: alerting can never break a payment flow (all failures
are swallowed and logged), and each alert key is throttled to once per hour
so an incident can't flood the inbox.
"""

import logging

import resend
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

THROTTLE_SECONDS = 3600


def alert_admin(key: str, subject: str, body: str) -> None:
    logger.error("ADMIN ALERT [%s]: %s — %s", key, subject, body)

    recipient = ""
    try:
        from .models import OpsConfig
        recipient = OpsConfig.get().alert_email
    except Exception:
        pass  # e.g. during migrations — fall through to the env value
    recipient = recipient or getattr(settings, "ADMIN_ALERT_EMAIL", "")
    if not recipient or not settings.RESEND_API_KEY:
        return  # log-only mode

    throttle_key = f"admin-alert:{key}"
    if cache.get(throttle_key):
        return
    cache.set(throttle_key, True, THROTTLE_SECONDS)

    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [recipient],
            "subject": f"[Azura ALERT] {subject}",
            "html": f"<pre style='font-family:monospace'>{body}</pre>",
        })
    except Exception:
        logger.exception("Failed to deliver admin alert %s", key)
