"""
Daily reminder emails for confirmed installment bookings with an outstanding
balance, at 7 / 3 / 1 days before the effective payment deadline.

Idempotent via per-booking `reminders_sent` markers — safe to run more than
once a day, or after a missed day.

Deployment: one host cron line (or a supercronic sidecar), e.g.
  0 8 * * *  docker compose -f docker-compose.prod.yml exec -T web \\
             python manage.py send_payment_reminders
"""

import html
import logging

import resend
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import Booking

logger = logging.getLogger(__name__)

REMINDER_MARKS = [7, 3, 1]  # days before deadline


def _build_reminder_html(booking, days_left):
    deadline = booking.effective_payment_deadline.strftime("%B %d, %Y")
    dashboard_url = f"{settings.FRONTEND_URL}/dashboard"
    urgency = "final reminder" if days_left <= 1 else "reminder"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Payment {urgency} – {booking.reference}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(booking.first_name)}, your balance is due soon</p>
    <p style="margin:0 0 18px;color:#555;font-size:14px;line-height:1.6;">
      Your booking <strong>{booking.reference}</strong> has an outstanding balance of
      <strong>{booking.currency} {booking.balance}</strong>, due by <strong>{deadline}</strong>
      ({days_left} day{"s" if days_left != 1 else ""} away).
    </p>
    <p style="margin:0 0 24px;color:#555;font-size:13px;line-height:1.6;">
      Bookings not fully paid by the deadline may be cancelled and deposits may be
      forfeited under the cancellation policy you accepted at booking.
    </p>
    <a href="{dashboard_url}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Pay your balance</a>
  </td></tr>
</table></td></tr></table></body></html>"""


class Command(BaseCommand):
    help = "Send 7/3/1-day payment-deadline reminder emails (idempotent)."

    def handle(self, *args, **options):
        if not settings.RESEND_API_KEY:
            self.stderr.write("RESEND_API_KEY not set — cannot send reminders.")
            return

        resend.api_key = settings.RESEND_API_KEY
        today = timezone.now().date()
        sent = 0

        candidates = (
            Booking.objects.filter(status=Booking.Status.CONFIRMED)
            .select_related("package")
        )
        for booking in candidates:
            if booking.is_paid:
                continue
            deadline = booking.effective_payment_deadline
            if not deadline:
                continue
            days_left = (deadline - today).days
            # Pick the closest due marker: covers missed cron days too
            # (e.g. days_left=6 after a missed day-7 run still sends "7d").
            marker = next((m for m in REMINDER_MARKS if days_left <= m), None)
            if marker is None or days_left < 0:
                continue
            key = f"{marker}d"
            if key in (booking.reminders_sent or {}):
                continue

            try:
                resend.Emails.send({
                    "from": settings.RESEND_FROM_EMAIL,
                    "to": [booking.email],
                    "subject": f"Balance due {deadline.strftime('%b %d')} – {booking.reference} | Azura Travels",
                    "html": _build_reminder_html(booking, days_left),
                })
            except Exception:
                logger.exception("Reminder email failed for %s", booking.reference)
                continue

            booking.reminders_sent = {**(booking.reminders_sent or {}), key: timezone.now().isoformat()}
            booking.save(update_fields=["reminders_sent", "updated_at"])
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
