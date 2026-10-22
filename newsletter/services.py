import html
import logging

import resend
from django.conf import settings

from packages.models import TravelPackage

from .models import NewsletterSubscriber

logger = logging.getLogger(__name__)


def _build_announcement_html(package: TravelPackage) -> str:
    from payments.email import _button, _h, _p, _shell, _summary, _trip_dates, _url

    rows = [("Duration", f"{package.duration_days} day{'s' if package.duration_days != 1 else ''}", "")]
    if package.available_from:
        rows.append(("Dates", _trip_dates(package.available_from, package.available_to), ""))
    if package.from_price:
        rows.append(("From", f"{package.currency} {package.from_price}", "strong"))
    body = (_h(html.escape(package.title))
            + _p(html.escape(package.description))
            + _summary(rows)
            + _button(_url(f"/tour/{package.id}"), "View the trip"))
    footer = ('You are receiving this because you subscribed to Azura Travels updates. '
              '<a href="{{UNSUBSCRIBE_URL}}" style="color:#6B7280;">Unsubscribe</a>.')
    return _shell(f"New: {package.title}", body, hero="saved.jpg", footer=footer)


UNSUBSCRIBE_SALT = "newsletter-unsubscribe"


def unsubscribe_url(email: str) -> str:
    from django.core import signing
    from django.urls import reverse
    token = signing.dumps({"e": email}, salt=UNSUBSCRIBE_SALT)
    base = getattr(settings, "BACKEND_URL", "") or settings.FRONTEND_URL.rstrip("/").replace("azuratravels.live", "azuratravelsbackend.cc")
    return f"{base}{reverse('newsletter-unsubscribe')}?token={token}"


def send_new_package_announcement(package_id) -> None:
    package = (
        TravelPackage.objects.prefetch_related("destinations")
        .filter(id=package_id)
        .first()
    )
    if not package:
        logger.warning("Package announcement skipped. Package not found id=%s", package_id)
        return

    recipients = list(
        NewsletterSubscriber.objects.filter(is_active=True).values_list("email", flat=True)
    )
    if not recipients:
        logger.info("Package announcement skipped. No active newsletter subscribers.")
        return

    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send package announcements.")
        return

    resend.api_key = settings.RESEND_API_KEY
    html = _build_announcement_html(package)
    subject = f"New Travel Package: {package.title}"

    # Send in batches of 100 (Resend batch API limit) — each recipient gets
    # an individually-addressed email, so addresses are never exposed.
    sent_count = 0
    failed_count = 0
    BATCH_SIZE = 100

    for start in range(0, len(recipients), BATCH_SIZE):
        chunk = recipients[start : start + BATCH_SIZE]
        batch_params = [
            {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [recipient],
                "subject": subject,
                # Privacy Policy: every marketing email carries a one-click,
                # per-recipient unsubscribe link (signed, so it can't be forged).
                "html": html.replace("{{UNSUBSCRIBE_URL}}", unsubscribe_url(recipient)),
            }
            for recipient in chunk
        ]
        try:
            resend.Batch.send(batch_params)
            sent_count += len(chunk)
        except Exception as exc:
            failed_count += len(chunk)
            logger.exception(
                "Failed sending package announcement batch (%s recipients) for package %s: %s",
                len(chunk),
                package.id,
                str(exc),
            )

    logger.info(
        "Package announcement completed for package id=%s sent=%s failed=%s",
        package.id,
        sent_count,
        failed_count,
    )
