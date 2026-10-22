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

from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import Booking

logger = logging.getLogger(__name__)

REMINDER_MARKS = [14, 7, 1]  # days before deadline (Installment Policy)
TRIP_REMINDER_MARKS = [30, 14, 7, 1]  # days before the first event day (v4 §6)


def _balance_box(booking):
    """Canvas: Emails · balance reminder — amount due and a paid progress bar."""
    from payments.email import _money

    total = booking.total_amount or 0
    pct = max(1, min(99, int(booking.amount_paid * 100 / total))) if total else 1
    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="background:#FAF5EB;border:1px solid #F3E7CF;border-radius:10px;margin:0 0 18px;">
  <tr><td style="padding:16px;">
    <p style="margin:0 0 4px;font-size:12px;color:#6B4E1A;">Balance due</p>
    <p style="margin:0 0 10px;font-family:Montserrat,Arial,sans-serif;font-size:24px;font-weight:700;color:#060504;">{_money(booking.currency, booking.balance)}</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#EADBC0;border-radius:999px;"><tr>
      <td width="{pct}%" style="background:#BD8F3A;height:8px;border-radius:999px;font-size:0;line-height:0;">&nbsp;</td><td style="font-size:0;line-height:0;">&nbsp;</td>
    </tr></table>
    <p style="margin:8px 0 0;font-size:12px;color:#374151;">Paid {_money(booking.currency, booking.amount_paid)} of {_money(booking.currency, total)}</p>
  </td></tr></table>"""


def reminder_subject(booking, days_left, trip):
    when = "today" if days_left <= 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
    if trip:
        start = booking.package.available_from
        lead = "Final reminder: your" if days_left <= 1 else "Your"
        return (f"{lead} balance is due before {start.day} {start:%b} – "
                f"{booking.reference} | Azura Travels")
    return f"Your balance is due {when} – {booking.reference} | Azura Travels"


def _build_trip_reminder_html(booking, days_left):
    """Trip bookings: everything is due before the first activity; unpaid
    guests can't take part, so the email lists each guest still owing."""
    from payments.email import _button, _h, _money, _p, _shell, _summary, _trip_dates, _url

    start = _trip_dates(booking.package.available_from)
    owing = [(t, owed) for t, _, _, owed in booking.traveller_balances() if owed > 0]
    rows = [(html.escape(t.full_name), _money(booking.currency, owed), "") for t, owed in owing]
    if booking.pay_separately:
        rows = [(f'{name} · <a href="{_url(f"/pay/{t.id}")}" style="color:#8A6420;">pay link</a>', amount, s)
                for (t, _), (name, amount, s) in zip(owing, rows)]
    body = (
        _h(f"Your trip starts {'tomorrow' if days_left <= 1 else f'in {days_left} days'}, {html.escape(booking.first_name)}")
        + _p(f"Your remaining balance for booking <strong>{booking.reference}</strong> is due before the first activity on <strong>{start}</strong>.")
        + _balance_box(booking)
        + (_summary(rows) if rows else "")
        + _button(_url(f"/booking/{booking.reference}/pay"), "Pay balance")
        + _p("Guests with an unpaid balance at the start of the event can't receive a badge or join activities, and unpaid "
             "bookings are forfeited under the booking terms you accepted.", small=True)
    )
    return _shell(f"Balance reminder – {booking.reference}", body, footer="Already paid? You can ignore this message.")


def _build_reminder_html(booking, days_left):
    from payments.email import _button, _h, _p, _shell, _url

    deadline = booking.effective_payment_deadline
    body = (
        _h(f"A quick reminder, {html.escape(booking.first_name)}")
        + _p(f"Your remaining balance for booking <strong>{booking.reference}</strong> is due on "
             f"<strong>{deadline.day} {deadline:%B %Y}</strong>.")
        + _balance_box(booking)
        + _button(_url(f"/booking/{booking.reference}/pay"), "Pay balance")  # public pay page — no login needed
        + _p("Extra payments before the deadline are welcome any time. If the balance is still outstanding after the "
             "deadline we will contact you before anything changes, under the booking terms you accepted.", small=True)
    )
    return _shell(f"Balance reminder – {booking.reference}", body, footer="Already paid? You can ignore this message.")


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
            subject = reminder_subject(booking, days_left, trip)
            body = _build_trip_reminder_html(booking, days_left) if trip else _build_reminder_html(booking, days_left)
            if not deliver(f"reminder_{key}", booking.email, subject, body, booking=booking):
                continue

            booking.reminders_sent = {**(booking.reminders_sent or {}), key: timezone.now().isoformat()}
            booking.save(update_fields=["reminders_sent", "updated_at"])
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
