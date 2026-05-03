import logging

import resend
from django.conf import settings

from packages.models import TravelPackage

from .models import NewsletterSubscriber

logger = logging.getLogger(__name__)


def _build_announcement_html(package: TravelPackage) -> str:
    destination_names = ", ".join(package.destinations.values_list("name", flat=True)) or "Multiple destinations"
    package_url = f"https://azuratravels.live/packages/{package.slug}"
    duration = f"{package.duration_days} day{'s' if package.duration_days != 1 else ''}"
    price_from = f"{package.currency} {package.price_shared}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>New Package: {package.title}</title>
</head>
<body style="margin:0;padding:0;background:#f7f5ef;font-family:Arial,sans-serif;color:#1a1a2e;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0;background:#f7f5ef;">
    <tr>
      <td align="center">
        <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;">
          <tr>
            <td style="padding:28px 32px;background:#1a1a2e;color:#ffffff;">
              <h1 style="margin:0;font-size:24px;line-height:1.3;">New Package Just Dropped</h1>
              <p style="margin:8px 0 0;font-size:14px;color:#d9d9e7;">Azura Travels Newsletter</p>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 32px;">
              <h2 style="margin:0 0 10px;font-size:22px;color:#1a1a2e;">{package.title}</h2>
              <p style="margin:0 0 16px;font-size:14px;line-height:1.6;color:#555;">{package.description}</p>
              <p style="margin:0 0 8px;font-size:14px;"><strong>Destination:</strong> {destination_names}</p>
              <p style="margin:0 0 8px;font-size:14px;"><strong>Duration:</strong> {duration}</p>
              <p style="margin:0 0 24px;font-size:14px;"><strong>From:</strong> {price_from}</p>
              <a href="{package_url}" style="display:inline-block;padding:12px 18px;background:#bd8f3a;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:700;">View Package</a>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px;background:#faf8f5;color:#7a7a88;font-size:12px;">
              You are receiving this because you subscribed to travel updates.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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

    sent_count = 0
    failed_count = 0

    for recipient in recipients:
        try:
            params: resend.Emails.SendParams = {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [recipient],
                "subject": subject,
                "html": html,
            }
            resend.Emails.send(params)
            sent_count += 1
        except Exception as exc:
            failed_count += 1
            logger.exception(
                "Failed sending package announcement to %s for package %s: %s",
                recipient,
                package.id,
                str(exc),
            )

    logger.info(
        "Package announcement completed for package id=%s sent=%s failed=%s",
        package.id,
        sent_count,
        failed_count,
    )

