"""
Installment Policy: when a confirmed booking's balance is still outstanding
after the final payment deadline, send ONE overdue notice opening a cure
period (OVERDUE_CURE_HOURS). Nothing here cancels anything — after the cure
period the booking simply surfaces in admin for a staff decision.
Idempotent via Booking.overdue_notice_sent_at; safe to run daily or ad hoc.
"""

import html
import logging

import resend
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import OVERDUE_CURE_HOURS, Booking

logger = logging.getLogger(__name__)


def _build_overdue_html(booking, cure_until) -> str:
    deadline = booking.effective_payment_deadline.strftime("%B %d, %Y")
    cure = cure_until.strftime("%B %d, %Y at %H:%M GMT")
    dashboard_url = f"{settings.FRONTEND_URL.rstrip('/')}/booking/{booking.reference}/pay"  # public pay page — no login needed
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Balance overdue – {booking.reference}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(booking.first_name)}, your balance is overdue</p>
    <p style="margin:0 0 18px;color:#555;font-size:14px;line-height:1.6;">
      Booking <strong>{booking.reference}</strong> still has an outstanding balance of
      <strong>{booking.currency} {booking.balance}</strong>. It was due by <strong>{deadline}</strong>.
    </p>
    <p style="margin:0 0 18px;color:#555;font-size:14px;line-height:1.6;">
      You have <strong>{OVERDUE_CURE_HOURS} hours</strong> — until <strong>{cure}</strong> — to settle it.
      No late fee applies. If it remains unpaid after that, our team will contact you before
      any change is made to your booking, under the booking terms and refund policy you accepted.
    </p>
    <a href="{dashboard_url}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Pay your balance</a>
    <p style="margin:24px 0 0;color:#888;font-size:12px;line-height:1.6;">
      Need more time? Reply to this email or contact
      <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a>.
    </p>
  </td></tr>
</table></td></tr></table></body></html>"""


class Command(BaseCommand):
    help = f"Send the one-time overdue notice ({OVERDUE_CURE_HOURS}h cure period) for unpaid balances past the deadline."

    def handle(self, *args, **options):
        sent = 0
        candidates = (
            Booking.objects.filter(status=Booking.Status.CONFIRMED, overdue_notice_sent_at__isnull=True)
            .select_related("package")
        )
        for booking in candidates:
            if not booking.is_overdue:
                continue
            now = timezone.now()
            from payments.email import deliver
            if not deliver("overdue_notice", booking.email,
                           f"Action needed: balance overdue – {booking.reference} | Azura Travels",
                           _build_overdue_html(booking, now + timezone.timedelta(hours=OVERDUE_CURE_HOURS)),
                           booking=booking):
                continue
            booking.overdue_notice_sent_at = now
            booking.save(update_fields=["overdue_notice_sent_at", "updated_at"])
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"{sent} overdue notice(s) sent."))
