import io
import base64
import html
import logging

import qrcode
import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _generate_qr_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_html(booking, payment) -> str:
    package = booking.package
    # Escape everything that originates from user or CMS input before it lands
    # in the email HTML — these fields (names, special requests, package/
    # destination titles) are attacker-controllable at an unauthenticated
    # checkout, so raw f-string interpolation is an HTML-injection sink.
    destination_names = ", ".join(html.escape(destination.name) for destination in package.destinations.all()) or "N/A"
    package_title = html.escape(package.title)
    first_name = html.escape(booking.first_name)
    travel_date = booking.travel_date.strftime("%B %d, %Y")
    duration = f"{package.duration_days} day{'s' if package.duration_days != 1 else ''}"
    full_name = f"{first_name} {html.escape(booking.last_name)}"

    if booking.balance > 0:
        paid_badge = "DEPOSIT PAID" if booking.amount_paid > 0 else "PENDING"
        deadline = booking.effective_payment_deadline
        deadline_text = f" by {deadline.strftime('%B %d, %Y')}" if deadline else ""
        balance_line = (
            f'<p style="margin:4px 0 0;color:#b3261e;font-size:12px;font-weight:700;">'
            f'Balance: {booking.currency} {booking.balance}{deadline_text}</p>'
        )
    else:
        paid_badge = "PAID"
        balance_line = ""

    from .email import build_claim_token  # self-import safe at call time
    claim_url = f"{settings.FRONTEND_URL}/claim?token={build_claim_token(booking)}"

    if booking.special_requests:
        special_requests_section = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-left:4px solid #d4a843;padding:0 0 0 16px;margin-bottom:24px;">
          <tr><td>
            <p style="margin:0 0 6px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Special Requests</p>
            <p style="margin:0;color:#1a1a2e;font-size:13px;">{html.escape(booking.special_requests)}</p>
          </td></tr>
        </table>"""
    else:
        special_requests_section = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Booking Confirmed – {booking.reference}</title>
</head>
<body style="margin:0;padding:0;background-color:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0ece4;padding:40px 0;">
  <tr><td align="center">
  <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

    <!-- ── Header ── -->
    <tr>
      <td style="background-color:#1a1a2e;padding:36px 40px 28px;border-radius:16px 16px 0 0;text-align:center;">
        <h1 style="color:#d4a843;margin:0;font-size:30px;font-weight:800;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
        <p style="color:#8888aa;margin:8px 0 0;font-size:12px;letter-spacing:2px;text-transform:uppercase;">Premium Travel Experiences</p>
      </td>
    </tr>

    <!-- ── Success Banner ── -->
    <tr>
      <td style="background:linear-gradient(135deg,#d4a843 0%,#f0c060 100%);padding:22px 40px;text-align:center;">
        <p style="margin:0;color:#1a1a2e;font-size:20px;font-weight:800;">&#10003; Booking Confirmed!</p>
        <p style="margin:6px 0 0;color:#1a1a2e;font-size:14px;opacity:0.85;">Your adventure awaits, {first_name}!</p>
      </td>
    </tr>

    <!-- ── Body ── -->
    <tr>
      <td style="background-color:#ffffff;padding:40px 40px 32px;">

        <!-- Ticket card -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:2px solid #e8e0d0;border-radius:14px;overflow:hidden;margin-bottom:28px;">

          <!-- Ticket header: reference -->
          <tr>
            <td style="background-color:#1a1a2e;padding:18px 28px;">
              <p style="margin:0;color:#d4a843;font-size:10px;letter-spacing:2px;text-transform:uppercase;">Booking Reference</p>
              <p style="margin:6px 0 0;color:#ffffff;font-size:24px;font-weight:800;letter-spacing:4px;">{booking.reference}</p>
            </td>
          </tr>

          <!-- Ticket body: details + QR -->
          <tr>
            <td style="padding:28px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <!-- Left: trip info -->
                  <td width="58%" valign="top" style="padding-right:24px;">
                    <p style="margin:0 0 3px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Trip</p>
                    <p style="margin:0 0 22px;color:#1a1a2e;font-size:15px;font-weight:700;line-height:1.4;">{package_title}</p>

                    <table cellpadding="0" cellspacing="0">
                      <tr>
                        <td style="padding-right:24px;padding-bottom:16px;vertical-align:top;">
                          <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Destination</p>
                          <p style="margin:0;color:#1a1a2e;font-size:13px;font-weight:600;">{destination_names}</p>
                        </td>
                        <td style="padding-bottom:16px;vertical-align:top;">
                          <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Duration</p>
                          <p style="margin:0;color:#1a1a2e;font-size:13px;font-weight:600;">{duration}</p>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding-right:24px;padding-bottom:16px;vertical-align:top;">
                          <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Travel Date</p>
                          <p style="margin:0;color:#1a1a2e;font-size:13px;font-weight:600;">{travel_date}</p>
                        </td>
                        <td style="padding-bottom:16px;vertical-align:top;">
                          <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Guests</p>
                          <p style="margin:0;color:#1a1a2e;font-size:13px;font-weight:600;">{booking.num_guests}</p>
                        </td>
                      </tr>
                      <tr>
                        <td colspan="2" style="vertical-align:top;">
                          <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Passenger</p>
                          <p style="margin:0;color:#1a1a2e;font-size:13px;font-weight:600;">{full_name}</p>
                        </td>
                      </tr>
                    </table>
                  </td>

                  <!-- Right: QR code -->
                  <td width="42%" valign="middle" align="center">
                    <table cellpadding="0" cellspacing="0" align="center"
                           style="border:1px solid #e8e0d0;border-radius:10px;background:#fff;">
                      <tr>
                        <td style="padding:14px;">
                          <img src="cid:qr-code"
                               width="130" height="130" alt="QR Code"
                               style="display:block;border-radius:4px;">
                          <p style="margin:10px 0 0;color:#aaa;font-size:9px;text-align:center;letter-spacing:1.5px;text-transform:uppercase;">Scan to Verify</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Tear line -->
          <tr>
            <td style="border-top:2px dashed #e8e0d0;background-color:#faf8f5;padding:18px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td valign="middle">
                    <p style="margin:0 0 2px;color:#aaa;font-size:10px;text-transform:uppercase;letter-spacing:1px;">Amount Paid</p>
                    <p style="margin:0;color:#1a1a2e;font-size:20px;font-weight:800;">{booking.currency} {booking.amount_paid}</p>
                    {balance_line}
                  </td>
                  <td align="right" valign="middle">
                    <span style="background-color:#16a34a;color:#ffffff;padding:6px 16px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:1px;">{paid_badge}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>

        <!-- Special requests -->
        {special_requests_section}

        <!-- What's next -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background-color:#faf8f5;border-radius:10px;margin-bottom:8px;">
          <tr>
            <td style="padding:24px;">
              <p style="margin:0 0 14px;color:#1a1a2e;font-size:14px;font-weight:700;">What happens next?</p>
              <p style="margin:0 0 10px;color:#555;font-size:13px;line-height:1.6;">&#128179; Track payments, download receipts and make top-up payments any time on
                <a href="{claim_url}" style="color:#d4a843;text-decoration:none;font-weight:600;">your booking dashboard</a>.
              </p>
              <p style="margin:0 0 10px;color:#555;font-size:13px;line-height:1.6;">&#128222; Our team will reach out within 24 hours to confirm your trip details.</p>
              <p style="margin:0 0 10px;color:#555;font-size:13px;line-height:1.6;">&#128203; A full itinerary will be shared with you before departure.</p>
              <p style="margin:0;color:#555;font-size:13px;line-height:1.6;">&#10067; Questions? Email us at
                <a href="mailto:hello@azuratravels.live" style="color:#d4a843;text-decoration:none;font-weight:600;">hello@azuratravels.live</a>
              </p>
            </td>
          </tr>
        </table>

      </td>
    </tr>

    <!-- ── Footer ── -->
    <tr>
      <td style="background-color:#1a1a2e;padding:24px 40px;border-radius:0 0 16px 16px;text-align:center;">
        <p style="margin:0 0 6px;color:#8888aa;font-size:12px;">
          &copy; 2025 Azura Travels &nbsp;&middot;&nbsp;
          <a href="https://azuratravels.live" style="color:#d4a843;text-decoration:none;">azuratravels.live</a>
        </p>
        <p style="margin:0;color:#555566;font-size:11px;">Please keep this email as your booking receipt.</p>
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""


def send_booking_confirmation(booking, payment) -> None:
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send confirmation email.")
        return

    resend.api_key = settings.RESEND_API_KEY

    try:
        qr_bytes = _generate_qr_bytes(f"https://azuratravels.live/booking/{booking.reference}")
        html = _build_html(booking, payment)

        params: resend.Emails.SendParams = {
            "from": settings.RESEND_FROM_EMAIL,
            "to": [booking.email],
            "subject": f"Booking Confirmed – {booking.reference} | Azura Travels",
            "html": html,
            "attachments": [
                {
                    "filename": "qr-code.png",
                    "content": list(qr_bytes),  # Resend SDK often prefers a list of ints for raw content
                    "content_type": "image/png",
                    "content_id": "qr-code",
                }
            ],
        }

        email = resend.Emails.send(params)
        logger.info("Confirmation email sent id=%s to=%s booking=%s", email.get("id"), booking.email, booking.reference)
    except Exception as exc:
        logger.exception("Failed to send confirmation email for booking %s: %s", booking.reference, str(exc))


def build_claim_token(booking) -> str:
    """Signed token that lets an authenticated account claim this booking on
    the dashboard. Possession of the emailed link is the proof of ownership —
    email-address match alone never grants access."""
    from django.core import signing

    return signing.dumps({"booking": str(booking.id)}, salt="booking-claim")


def _payment_summary_rows(booking, payment) -> str:
    deadline = booking.effective_payment_deadline
    deadline_row = ""
    if booking.balance > 0 and deadline:
        deadline_row = f"""
          <tr><td style="padding:6px 0;color:#888;font-size:13px;">Final payment deadline</td>
              <td align="right" style="padding:6px 0;color:#b3261e;font-size:13px;font-weight:700;">{deadline.strftime("%B %d, %Y")}</td></tr>"""
    return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0;">
          <tr><td style="padding:6px 0;color:#888;font-size:13px;">Payment received</td>
              <td align="right" style="padding:6px 0;color:#1a1a2e;font-size:13px;font-weight:700;">{payment.currency} {payment.amount}</td></tr>
          <tr><td style="padding:6px 0;color:#888;font-size:13px;">Total booking amount</td>
              <td align="right" style="padding:6px 0;color:#1a1a2e;font-size:13px;">{booking.currency} {booking.total_amount}</td></tr>
          <tr><td style="padding:6px 0;color:#888;font-size:13px;">Paid to date</td>
              <td align="right" style="padding:6px 0;color:#1a1a2e;font-size:13px;">{booking.currency} {booking.amount_paid}</td></tr>
          <tr><td style="padding:6px 0;color:#888;font-size:13px;border-top:1px solid #eee;">Remaining balance</td>
              <td align="right" style="padding:6px 0;color:#1a1a2e;font-size:15px;font-weight:800;border-top:1px solid #eee;">{booking.currency} {booking.balance}</td></tr>
          {deadline_row}
        </table>"""


def send_payment_receipt(booking, payment) -> None:
    """
    Receipt for a successful payment that did not newly confirm the booking
    (installment top-ups, balance payments). Confirmation emails are sent by
    send_booking_confirmation on promotion only.
    """
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send payment receipt.")
        return

    resend.api_key = settings.RESEND_API_KEY

    fully_paid = booking.is_paid
    heading = "Payment Complete — Fully Paid!" if fully_paid else "Payment Received"
    sub = (
        "Your booking is now fully paid. We can't wait to host you!"
        if fully_paid
        else "Thanks — your installment payment has been applied to your booking."
    )
    dashboard_url = f"{settings.FRONTEND_URL}/dashboard"

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Receipt – {booking.reference}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="background:linear-gradient(135deg,#d4a843 0%,#f0c060 100%);padding:18px 40px;text-align:center;">
    <p style="margin:0;color:#1a1a2e;font-size:18px;font-weight:800;">&#10003; {heading}</p>
    <p style="margin:6px 0 0;color:#1a1a2e;font-size:13px;opacity:0.85;">{sub}</p>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 4px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Booking Reference</p>
    <p style="margin:0 0 16px;color:#1a1a2e;font-size:20px;font-weight:800;">{booking.reference}</p>
    <p style="margin:0 0 4px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Payment Reference</p>
    <p style="margin:0;color:#1a1a2e;font-size:13px;">{payment.paystack_reference or f"offline:{str(payment.id)[:8]}"}</p>
    {_payment_summary_rows(booking, payment)}
    <a href="{dashboard_url}" style="display:inline-block;padding:12px 20px;background:#1a1a2e;color:#d4a843;text-decoration:none;border-radius:999px;font-weight:700;font-size:13px;">View your booking &amp; payment history</a>
  </td></tr>
  <tr><td style="padding:16px 40px;background:#faf8f5;color:#7a7a88;font-size:11px;">
    Keep this email as your receipt. Questions? Just reply to this email.
  </td></tr>
</table></td></tr></table></body></html>"""

    try:
        email = resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [booking.email],
            "subject": (
                f"Payment Complete – {booking.reference} | Azura Travels"
                if fully_paid else
                f"Payment Received – {booking.reference} | Azura Travels"
            ),
            "html": html,
        })
        logger.info("Receipt email sent id=%s to=%s booking=%s", email.get("id"), booking.email, booking.reference)
    except Exception as exc:
        logger.exception("Failed to send receipt email for booking %s: %s", booking.reference, str(exc))
