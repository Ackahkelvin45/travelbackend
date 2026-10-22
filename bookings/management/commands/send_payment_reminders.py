"""
Daily reminder emails for confirmed bookings with an outstanding balance:
  - trip (component) bookings: 30 / 14 / 7 days before the event starts, then
    a final operational reminder the day before (Change Request v4 §6);
  - other bookings: 14 / 7 / 1 days before the effective payment deadline.

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

REMINDER_MARKS = [14, 7, 1]  # days before deadline (Installment Policy)
TRIP_REMINDER_MARKS = [30, 14, 7, 1]  # days before the first event day (v4 §6)


def _build_trip_reminder_html(booking, days_left):
    """Trip bookings: everything is due before the first activity; unpaid
    guests can't take part, so the email lists each guest still owing."""
    start = booking.package.available_from.strftime("%A %d %B %Y")
    pay_url = f"{settings.FRONTEND_URL.rstrip('/')}/booking/{booking.reference}/pay"
    final = days_left <= 1
    owing = [(t, owed) for t, _, _, owed in booking.traveller_balances() if owed > 0]
    rows = "".join(
        f'<tr><td style="padding:4px 0;font-size:13px;">{html.escape(t.full_name)}</td>'
        f'<td align="right" style="padding:4px 0;font-size:13px;">{booking.currency} {owed:,.2f}</td></tr>'
        for t, owed in owing
    )
    links = "".join(
        f'<li style="margin:0 0 4px;"><a href="{settings.FRONTEND_URL.rstrip("/")}/pay/{t.id}">{html.escape(t.full_name)}\'s pay link</a></li>'
        for t, _ in owing
    ) if booking.pay_separately else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Balance reminder – {booking.reference}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(booking.first_name)}, {"your trip starts tomorrow" if final else f"your trip starts in {days_left} days"}</p>
    <p style="margin:0 0 14px;color:#555;font-size:14px;line-height:1.6;">
      Booking <strong>{booking.reference}</strong> has <strong>{booking.currency} {booking.balance:,.2f}</strong> outstanding.
      Everything must be paid before the first activity on <strong>{start}</strong>.
    </p>
    <table cellpadding="0" cellspacing="0" style="width:100%;margin:0 0 14px;">{rows}</table>
    {f'<ul style="margin:0 0 14px;padding-left:18px;font-size:13px;">{links}</ul>' if links else ""}
    <p style="margin:0 0 24px;color:#555;font-size:13px;line-height:1.6;">
      Guests with an unpaid balance at the start of the event can't receive a badge or join activities, and unpaid
      bookings are forfeited under the booking terms you accepted. Contact us if you need help.
    </p>
    <a href="{pay_url}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Pay now</a>
  </td></tr>
</table></td></tr></table></body></html>"""


def _build_reminder_html(booking, days_left):
    deadline = booking.effective_payment_deadline.strftime("%B %d, %Y")
    dashboard_url = f"{settings.FRONTEND_URL.rstrip('/')}/booking/{booking.reference}/pay"  # public pay page — no login needed
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
      Extra payments before the deadline are welcome any time. If the balance is still
      outstanding after the deadline we will contact you before anything changes, under
      the booking terms and refund policy you accepted at booking.
    </p>
    <a href="{dashboard_url}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Pay your balance</a>
  </td></tr>
</table></td></tr></table></body></html>"""


class Command(BaseCommand):
    help = "Send 14/7/1-day payment-deadline reminder emails (idempotent)."

    def handle(self, *args, **options):
        today = timezone.now().date()
        sent = 0

        candidates = (
            Booking.objects.filter(status=Booking.Status.CONFIRMED)
            .select_related("package")
        )
        for booking in candidates:
            if booking.is_paid:
                continue
            trip = bool(booking.mode)
            anchor = booking.package.available_from if trip else booking.effective_payment_deadline
            if not anchor:
                continue
            days_left = (anchor - today).days
            # Pick the closest due marker: covers missed cron days too
            # (e.g. days_left=6 after a missed day-7 run still sends "7d").
            marks = TRIP_REMINDER_MARKS if trip else REMINDER_MARKS
            marker = next((m for m in reversed(marks) if days_left <= m), None)
            if marker is None or days_left < 0:
                continue
            key = f"{marker}d"
            if key in (booking.reminders_sent or {}):
                continue

            from payments.email import deliver
            subject = (f"{'Final reminder: ' if marker == 1 else ''}balance due before {anchor.strftime('%d %b')} – "
                       f"{booking.reference} | Azura Travels") if trip else \
                f"Balance due {anchor.strftime('%b %d')} – {booking.reference} | Azura Travels"
            body = _build_trip_reminder_html(booking, days_left) if trip else _build_reminder_html(booking, days_left)
            if not deliver(f"reminder_{key}", booking.email, subject, body, booking=booking):
                continue

            booking.reminders_sent = {**(booking.reminders_sent or {}), key: timezone.now().isoformat()}
            booking.save(update_fields=["reminders_sent", "updated_at"])
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
