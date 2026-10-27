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

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

THROTTLE_SECONDS = 3600


def _booking_rows(b) -> list:
    from .email import _money
    return [("Booking", b.reference), ("Guest", b.full_name), ("Email", b.email), ("Phone", b.phone),
            ("Trip", b.package.title if b.package_id else ""), ("Status", b.get_status_display()),
            ("Paid", _money(b.currency, b.amount_paid)), ("Total", _money(b.currency, b.total_amount))]


def alert_admin(key: str, subject: str, body: str, *, rows=(), booking=None, link_obj=None) -> None:
    """body: what happened and what to do, in plain text. rows: extra
    (label, value) facts; a booking adds its own and the admin link."""
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
        from .email import admin_url, send_staff_alert
        target = link_obj or booking
        send_staff_alert(
            recipient, subject, body, [*rows, *(_booking_rows(booking) if booking else [])],
            kind="admin_alert", booking=booking,
            link=admin_url(target) if target else "", link_label="Open in admin",
        )
    except Exception:
        logger.exception("Failed to deliver admin alert %s", key)
