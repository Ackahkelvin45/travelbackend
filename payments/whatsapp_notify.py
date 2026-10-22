"""
WhatsApp payment confirmations (Change Request v4 §8): after each verified
payment, guests who opted in get a Meta-approved template message with their
reference, amount paid, balance and a link to their booking (receipt, tickets,
itinerary and extras). Business-initiated WhatsApp messages must use an
approved template, so nothing is sent until its name is set in Settings.
Best effort: email stays the record; a WhatsApp failure is only logged.
It runs from the same post-commit hook as the confirmation email, which fires
once per verified payment (a repeated webhook finds the payment applied).
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)



def _wa_id(phone) -> str:
    """Digits with country code, as Meta wants. Only numbers written with an
    international prefix (+233… or 00233…) qualify: a local number such as
    024… has no country code, so it's skipped rather than guessed."""
    raw = (phone or "").strip()
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not raw.startswith("+"):
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    return digits if 8 <= len(digits) <= 15 and not digits.startswith("0") else ""


def send_payment_whatsapp(booking) -> bool:
    """Send the payment template to an opted-in guest. Returns True if Meta accepted it."""
    from bookings.whatsapp import GRAPH_URL

    from .models import OpsConfig

    ops = OpsConfig.get()
    to = _wa_id(booking.phone)
    if not (booking.whatsapp_updates and to and ops.whatsapp_confirmation_template
            and settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_TOKEN):
        return False
    link = f"{settings.FRONTEND_URL.rstrip('/')}/payment/success?reference={booking.reference}&t={booking.view_token}"
    values = [booking.first_name or "there", booking.reference, f"{booking.currency} {booking.amount_paid:,.2f}",
              f"{booking.currency} {booking.balance:,.2f}", link]
    try:
        r = requests.post(
            f"{GRAPH_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
                "name": ops.whatsapp_confirmation_template,
                "language": {"code": ops.whatsapp_template_language or "en"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}],
            }},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException:
        # ponytail: failures are logged, not stored; add a delivery log if staff need to see them in admin.
        logger.exception("WhatsApp confirmation failed for booking %s", booking.reference)
        return False
    return True
