import io
import base64
import html
import logging
from decimal import Decimal

import qrcode
import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def deliver(kind: str, to: str, subject: str, html_body: str, *, booking=None, attachments=None) -> bool:
    """Send one customer email through Resend and record the attempt in
    EmailLog (Appendix C: staff must be able to see what was sent and whether
    the provider accepted it). Never raises."""
    from .models import EmailLog

    def _log(status, provider_id="", error=""):
        try:
            EmailLog.objects.create(booking=booking, kind=kind, to_email=to, subject=subject[:300],
                                    status=status, provider_id=provider_id or "", error=error[:2000])
        except Exception:
            logger.exception("EmailLog write failed for %s → %s", kind, to)

    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set. Cannot send %s.", kind)
        _log(EmailLog.Status.FAILED, error="RESEND_API_KEY not set")
        return False
    resend.api_key = settings.RESEND_API_KEY
    params = {"from": settings.RESEND_FROM_EMAIL, "to": [to], "subject": subject, "html": html_body}
    if attachments:
        params["attachments"] = attachments
    try:
        result = resend.Emails.send(params) or {}
        _log(EmailLog.Status.SENT, provider_id=str(result.get("id", "")))
        return True
    except Exception as exc:
        logger.exception("Failed to send %s to %s", kind, to)
        _log(EmailLog.Status.FAILED, error=str(exc))
        return False


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

    # Terms Part A: the confirmation identifies inclusions/add-ons and the
    # exact policy versions accepted, with durable links to each.
    from .receipts import compute_line_items
    items = compute_line_items(booking)
    rows = "".join(
        f'<tr><td style="padding:5px 0;color:#555;font-size:13px;">{html.escape(l["label"])}'
        f'<span style="display:block;color:#999;font-size:11px;">{html.escape(l["detail"])}</span></td>'
        f'<td align="right" style="padding:5px 0;color:#1a1a2e;font-size:13px;white-space:nowrap;">{booking.currency} {l["amount"]:,.2f}</td></tr>'
        for l in items["lines"]
    )
    if items["bundle_discount"] > 0:
        rows += (f'<tr><td style="padding:5px 0;color:#2e7d32;font-size:13px;">{html.escape(items["discount_note"] or "Bundle discount")}</td>'
                 f'<td align="right" style="padding:5px 0;color:#2e7d32;font-size:13px;">− {booking.currency} {items["bundle_discount"]:,.2f}</td></tr>')
    policy_paths = {"terms": "/terms-and-conditions", "refund": "/refund-policy",
                    "installment": "/installment-policy", "privacy": "/privacy-policy"}
    accepted = booking.policy_acceptances.select_related("document")
    policy_links = " · ".join(
        f'<a href="{settings.FRONTEND_URL.rstrip("/")}{policy_paths.get(a.document.type, "/terms-and-conditions")}" '
        f'style="color:#d4a843;text-decoration:none;">{html.escape(a.document.title)} v{html.escape(a.document.version)}</a>'
        for a in accepted
    )
    booked_section = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
          <tr><td>
            <p style="margin:0 0 8px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">What you booked</p>
            <table width="100%" cellpadding="0" cellspacing="0">{rows}
              <tr><td style="padding:8px 0 0;color:#1a1a2e;font-size:13px;font-weight:800;border-top:1px solid #eee;">Total</td>
                  <td align="right" style="padding:8px 0 0;color:#1a1a2e;font-size:13px;font-weight:800;border-top:1px solid #eee;">{booking.currency} {items["total"]:,.2f}</td></tr>
            </table>
            {f'<p style="margin:12px 0 0;color:#888;font-size:11px;line-height:1.6;">You accepted: {policy_links}. These versions form your booking contract.</p>' if policy_links else ""}
          </td></tr>
        </table>"""

    # Privacy Policy: special requests can hold dietary/medical details, so
    # they are never echoed into email — the team confirms them privately.
    if booking.special_requests:
        special_requests_section = """
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-left:4px solid #d4a843;padding:0 0 0 16px;margin-bottom:24px;">
          <tr><td>
            <p style="margin:0 0 6px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Special Requests</p>
            <p style="margin:0;color:#1a1a2e;font-size:13px;">We've noted your requests. Our team will confirm the arrangements with you directly.</p>
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

        <!-- What you booked (same breakdown as the receipt) -->
        {booked_section}

        <!-- Special requests -->
        {special_requests_section}

        <!-- What's next -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background-color:#faf8f5;border-radius:10px;margin-bottom:8px;">
          <tr>
            <td style="padding:24px;">
              <p style="margin:0 0 14px;color:#1a1a2e;font-size:14px;font-weight:700;">What happens next?</p>
              <p style="margin:0 0 10px;color:#555;font-size:13px;line-height:1.6;">&#128179; To track payments, download receipts and top up any time,
                <a href="{claim_url}" style="color:#d4a843;text-decoration:none;font-weight:600;">open your booking dashboard</a>
                and sign in — or create a free account — using <strong>this same email address</strong>. Your booking attaches to it automatically.
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
    try:
        qr_bytes = _generate_qr_bytes(f"{settings.FRONTEND_URL.rstrip('/')}/payment/callback?reference={booking.reference}")
        html_body = _build_html(booking, payment)
    except Exception:
        logger.exception("Could not build confirmation email for booking %s", booking.reference)
        return
    deliver(
        "confirmation", booking.email,
        f"Booking Confirmed – {booking.reference} | Azura Travels", html_body, booking=booking,
        attachments=[{
            "filename": "qr-code.png",
            "content": list(qr_bytes),  # Resend SDK prefers a list of ints for raw content
            "content_type": "image/png",
            "content_id": "qr-code",
        }],
    )


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
    fully_paid = booking.is_paid
    if payment.purpose == "addon":
        added = next((l.get("name") for l in (booking.addons or []) if l.get("code") == payment.addon_code), "Your experience")
        heading = f"{added} added"
        sub = "It's paid for and now part of your booking — see the updated details below."
    else:
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

    deliver("receipt", booking.email, f"Payment Receipt – {booking.reference} | Azura Travels", html, booking=booking)


# ── Cancellation request emails (Refund Policy, Part B) ──────────────────────

def _shell(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece4;padding:40px 0;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#1a1a2e;padding:28px 40px;text-align:center;">
    <h1 style="color:#d4a843;margin:0;font-size:24px;letter-spacing:3px;text-transform:uppercase;">Azura Travels</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;color:#555;font-size:14px;line-height:1.6;">{body_html}</td></tr>
</table></td></tr></table></body></html>"""


def _send(to: str, subject: str, html_body: str, what: str, booking=None) -> None:
    deliver(what.replace(" ", "_"), to, subject, html_body, booking=booking)


def send_cancellation_acknowledgment(req) -> None:
    """Dated acknowledgment — the timestamp here is the one the refund band is
    judged from, so the guest has it in writing immediately."""
    b = req.booking
    q = req.refund_quote or {}
    received = req.requested_at.strftime("%B %d, %Y at %H:%M GMT")
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, we've received your cancellation request</p>
    <p style="margin:0 0 14px;">Booking <strong>{b.reference}</strong> · request received <strong>{received}</strong>.
       Your refund entitlement is calculated from this moment, not from when we finish reviewing.</p>
    <table cellpadding="0" cellspacing="0" style="width:100%;margin:0 0 18px;font-size:13px;">
      <tr><td style="padding:6px 0;color:#888;">Amount paid</td><td align="right" style="padding:6px 0;">{b.currency} {q.get("net_paid", b.amount_paid)}</td></tr>
      <tr><td style="padding:6px 0;color:#888;">Days before departure</td><td align="right" style="padding:6px 0;">{q.get("days_before_departure", "—")}</td></tr>
      <tr><td style="padding:6px 0;color:#888;">Refund rate</td><td align="right" style="padding:6px 0;">{q.get("percent", "0")}%</td></tr>
      <tr><td style="padding:8px 0;color:#1a1a2e;font-weight:800;border-top:1px solid #eee;">Estimated refund</td>
          <td align="right" style="padding:8px 0;color:#1a1a2e;font-weight:800;border-top:1px solid #eee;">{b.currency} {q.get("refund_total", "0.00")}</td></tr>
    </table>
    <p style="margin:0 0 10px;font-size:13px;">No further payments will be collected while your request is under review.
       We aim to review within five business days and to initiate any approved refund within ten business days of approval,
       to your original payment method. Nothing else changes on your booking until we confirm the outcome.</p>
    <p style="margin:0;font-size:12px;color:#888;">Questions? <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a></p>"""
    _send(b.email, f"Cancellation request received – {b.reference} | Azura Travels",
          _shell("Cancellation request received", body), "cancellation acknowledgment", booking=b)


def send_cancellation_outcome(req) -> None:
    b = req.booking
    approved = req.status == "approved"
    q = req.refund_quote or {}
    note = f'<p style="margin:0 0 14px;">{html.escape(req.staff_note)}</p>' if req.staff_note else ""
    if approved:
        heading = "Your booking has been cancelled"
        body_text = (f"Booking <strong>{b.reference}</strong> is now cancelled as of your request on "
                     f"<strong>{req.requested_at.strftime('%B %d, %Y')}</strong>. "
                     f"Refund due: <strong>{b.currency} {q.get('refund_total', '0.00')}</strong> "
                     f"({q.get('percent', '0')}% of the amount paid), to your original payment method. "
                     "We'll email you the refund reference once it has been initiated; bank posting times vary.")
    else:
        heading = "Update on your cancellation request"
        body_text = (f"We were unable to approve the cancellation request for booking <strong>{b.reference}</strong>. "
                     "Your booking remains active and payments can continue as before.")
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, {heading.lower()}</p>
    <p style="margin:0 0 14px;">{body_text}</p>
    {note}
    <p style="margin:0;font-size:12px;color:#888;">Questions? <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a></p>"""
    _send(b.email, f"{heading} – {b.reference} | Azura Travels", _shell(heading, body), "cancellation outcome", booking=b)


def send_refund_processed(refund) -> None:
    """The refund has been initiated: amount, date, reference, where it goes."""
    b = refund.booking
    when = (refund.processed_at or timezone_now()).strftime("%B %d, %Y")
    via = "your original payment method" if refund.payment and refund.payment.method == "paystack" else "bank transfer"
    gateway = (refund.breakdown or {}).get("execute_on_gateway")
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, your refund is on its way</p>
    <p style="margin:0 0 14px;">We've initiated a refund for booking <strong>{b.reference}</strong>.</p>
    <table cellpadding="0" cellspacing="0" style="width:100%;margin:0 0 18px;font-size:13px;">
      <tr><td style="padding:6px 0;color:#888;">Amount</td><td align="right" style="padding:6px 0;font-weight:800;color:#1a1a2e;">{refund.currency} {refund.amount}</td></tr>
      {f'<tr><td style="padding:6px 0;color:#888;">Charged as</td><td align="right" style="padding:6px 0;">{html.escape(gateway)}</td></tr>' if gateway else ""}
      <tr><td style="padding:6px 0;color:#888;">Initiated</td><td align="right" style="padding:6px 0;">{when}</td></tr>
      <tr><td style="padding:6px 0;color:#888;">Refund reference</td><td align="right" style="padding:6px 0;">{html.escape(refund.external_reference or "—")}</td></tr>
      <tr><td style="padding:6px 0;color:#888;">Returned to</td><td align="right" style="padding:6px 0;">{via}</td></tr>
    </table>
    <p style="margin:0 0 10px;font-size:13px;">Banks and card providers post refunds at different speeds — usually within 5–10 business days.
       Quote the reference above if you need to ask us or your bank about it.</p>
    <p style="margin:0;font-size:12px;color:#888;">Questions? <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a></p>"""
    _send(b.email, f"Refund initiated – {b.reference} | Azura Travels", _shell("Refund initiated", body), "refund processed", booking=b)


def timezone_now():
    from django.utils import timezone
    return timezone.now()


def send_payment_failed(payment) -> None:
    """A card/MoMo attempt failed. Nothing was charged; here's the safe retry."""
    b = payment.booking
    pay_url = f"{settings.FRONTEND_URL.rstrip('/')}/booking/{b.reference}/pay"
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, a payment didn't go through</p>
    <p style="margin:0 0 14px;">Your attempt to pay <strong>{payment.currency} {payment.amount}</strong> towards booking
       <strong>{b.reference}</strong> was declined by your bank or provider. <strong>Nothing was charged</strong>, and your booking is unchanged.</p>
    <p style="margin:0 0 18px;">You can try again whenever you're ready — a fresh, secure payment session is created each time.</p>
    <a href="{pay_url}" style="display:inline-block;padding:14px 24px;background:#d4a843;color:#1a1a2e;text-decoration:none;border-radius:999px;font-weight:800;font-size:14px;">Try again</a>
    <p style="margin:24px 0 0;font-size:12px;color:#888;">If this keeps happening, check with your bank or contact <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a>.</p>"""
    deliver("payment_failed", b.email, f"Payment not completed – {b.reference} | Azura Travels",
            _shell("Payment not completed", body), booking=b)


def send_traveller_updated(booking, previous: dict, kind: str) -> None:
    """Terms Part A: confirm a name correction or traveller replacement in
    writing. The new traveller gets the booking link and a claim link for
    their own account; a replaced traveller's old address is told too."""
    b = booking
    status_url = f"{settings.FRONTEND_URL.rstrip('/')}/payment/callback?reference={b.reference}"
    claim_url = f"{settings.FRONTEND_URL.rstrip('/')}/claim?token={build_claim_token(b)}"
    what = "name corrected" if kind == "correction" else "traveller updated"
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, your booking details were updated</p>
    <p style="margin:0 0 14px;">Booking <strong>{b.reference}</strong> · {html.escape(b.package.title)} now names
       <strong>{html.escape(b.first_name)} {html.escape(b.last_name)}</strong> as the traveller
       {"(previously " + html.escape(previous["first_name"] + " " + previous["last_name"]) + ")" if kind == "replacement" else "(spelling corrected)"}.
       All payments made stay with this booking. No charge applies for this change.</p>
    <p style="margin:0 0 18px;">You can check the booking any time at
       <a href="{status_url}" style="color:#d4a843;">{status_url}</a>.
       To manage payments and receipts in your own account, <a href="{claim_url}" style="color:#d4a843;font-weight:600;">open the booking dashboard</a>
       and sign in or create an account with this email address.</p>
    <p style="margin:0;font-size:12px;color:#888;">Didn't expect this? Contact <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a> immediately.</p>"""
    deliver(f"traveller_{kind}", b.email, f"Booking {what} – {b.reference} | Azura Travels", _shell("Booking updated", body), booking=b)

    if previous.get("email") and previous["email"].lower() != b.email.lower():
        old_body = f"""
        <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Booking {b.reference} has been transferred</p>
        <p style="margin:0 0 14px;">At the request received by our team, booking <strong>{b.reference}</strong> now names a different traveller,
           and this email address is no longer attached to it. If you did not ask for this, contact us immediately at
           <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a>.</p>"""
        deliver("traveller_replaced_notice", previous["email"], f"Booking {b.reference} transferred | Azura Travels",
                _shell("Booking transferred", old_body), booking=b)


def send_addon_removed(booking, line: dict, fee, refund) -> None:
    b = booking
    name = html.escape(line.get("name", "an add-on"))
    charge = line.get("line_total", "0")
    outcome = (
        f"You'd already paid more than the new total, so <strong>{b.currency} {refund}</strong> is being refunded to your original payment method — we'll email the reference once it's initiated."
        if refund and Decimal(str(refund)) > 0 else
        f"Your remaining balance is now <strong>{b.currency} {b.balance}</strong>."
    )
    body = f"""
    <p style="margin:0 0 12px;color:#1a1a2e;font-size:18px;font-weight:800;">Hi {html.escape(b.first_name)}, {name} has been removed</p>
    <p style="margin:0 0 14px;">As requested, <strong>{name}</strong> ({b.currency} {charge}) has been cancelled on booking <strong>{b.reference}</strong>.
       Under the booking terms, 50% of a cancelled experience is retained: a cancellation fee of <strong>{b.currency} {fee}</strong> applies.</p>
    <p style="margin:0 0 14px;">New booking total: <strong>{b.currency} {b.total_amount}</strong>. {outcome}</p>
    <p style="margin:0;font-size:12px;color:#888;">Questions? <a href="mailto:hello@azuratravels.live" style="color:#d4a843;">hello@azuratravels.live</a></p>"""
    deliver("addon_removed", b.email, f"{line.get('name', 'Add-on')} cancelled – {b.reference} | Azura Travels",
            _shell("Add-on cancelled", body), booking=b)
