import html
import logging
from decimal import Decimal

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def deliver(kind: str, to: str, subject: str, html_body: str, *, booking=None, lead=None, attachments=None) -> bool:
    """Send one customer email through Resend and record the attempt in
    EmailLog (Appendix C: staff must be able to see what was sent and whether
    the provider accepted it). Never raises."""
    from .models import EmailLog

    def _log(status, provider_id="", error=""):
        try:
            EmailLog.objects.create(booking=booking, lead=lead, kind=kind, to_email=to, subject=subject[:300],
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
    ops = _ops()
    if ops and ops.contact_email:
        params["reply_to"] = ops.contact_email  # "Reply to this email" reaches the team
    try:
        result = resend.Emails.send(params) or {}
        _log(EmailLog.Status.SENT, provider_id=str(result.get("id", "")))
        return True
    except Exception as exc:
        logger.exception("Failed to send %s to %s", kind, to)
        _log(EmailLog.Status.FAILED, error=str(exc))
        return False


# ── Brand layout (canvas: Emails) ─────────────────────────────────────────────
# Every guest email: grey page, white card, Azura logo, optional photo, gold
# button, contact footer. Images are served by the frontend (public/email/).

_FONT = "font-family:'Open Sans',Arial,Helvetica,sans-serif;"
_HEAD = "font-family:Raleway,'Helvetica Neue',Arial,sans-serif;"
_LINK = "color:#8A6420;"


def _ops():
    from .models import OpsConfig
    try:
        return OpsConfig.get()
    except Exception:
        logger.exception("OpsConfig unavailable for email footer")
        return None


def _url(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def _h(text: str) -> str:
    """Card heading. `text` must already be escaped."""
    return f'<h2 style="margin:0 0 10px;{_HEAD}font-size:22px;font-weight:700;color:#060504;">{text}</h2>'


def _p(text: str, *, small: bool = False) -> str:
    size, color = ("12px", "#6B7280") if small else ("14px", "#374151")
    return f'<p style="margin:0 0 14px;font-size:{size};line-height:1.6;color:{color};">{text}</p>'


def _button(href: str, label: str) -> str:
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 18px;"><tr><td align="center" '
            f'style="background:#BD8F3A;border-radius:10px;"><a href="{html.escape(href)}" style="display:block;padding:13px 16px;'
            f'font-family:Montserrat,Arial,sans-serif;font-weight:600;font-size:14px;color:#FFFFFF;text-decoration:none;">{label}</a>'
            f'</td></tr></table>')


def _summary(rows) -> str:
    """Grey summary box. rows: (label, value, style) with escaped text; style
    is "" (normal), "strong", or "saving" (green)."""
    out = ""
    for label, value, style in rows:
        color = "#15803D" if style == "saving" else "#060504"
        weight = "700" if style == "strong" else "400"
        out += (f'<tr><td style="padding:4px 0;font-size:13px;color:{color};">{label}</td>'
                f'<td align="right" style="padding:4px 0;font-size:13px;color:{color};font-weight:{weight};white-space:nowrap;">{value}</td></tr>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;border-radius:10px;margin:0 0 18px;">'
            f'<tr><td style="padding:12px 14px;"><table width="100%" cellpadding="0" cellspacing="0">{out}</table></td></tr></table>')


def _contact_footer() -> str:
    """Only details staff have confirmed in Settings; never placeholders."""
    ops = _ops()
    ask = "Questions? Reply to this email"
    if ops and ops.whatsapp_number:
        ask += f" or WhatsApp {html.escape(ops.whatsapp_number)}"
    lines = [ask + "."]
    if ops and (ops.legal_name or ops.legal_address):
        lines.append(" · ".join(html.escape(x) for x in (ops.legal_name, " ".join(ops.legal_address.split())) if x))
    return "<br>".join(lines)


def _shell(title: str, body_html: str, *, hero: str = "", footer: str = "") -> str:
    """Wrap a body in the brand layout. `hero` is a public/email/ file name;
    `footer` is extra escaped text shown above the contact line."""
    hero_html = (f'<tr><td style="padding:0 0 18px;"><img src="{_url("/email/" + hero)}" width="536" alt="" '
                 f'style="display:block;width:100%;max-width:536px;height:auto;border-radius:10px;"></td></tr>') if hero else ""
    foot = (footer + "<br>" if footer else "") + _contact_footer()
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#EEF0F2;{_FONT}color:#060504;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EEF0F2;padding:32px 12px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:12px;">
  <tr><td style="padding:28px 32px 8px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:0 0 18px;"><a href="{_url("/")}"><img src="{_url("/email/logo.png")}" width="110" alt="Azura Travels" style="display:block;width:110px;height:auto;border:0;"></a></td></tr>
      {hero_html}
      <tr><td style="{_FONT}">{body_html}</td></tr>
    </table>
  </td></tr>
  <tr><td style="padding:0 32px 26px;font-size:12px;line-height:1.6;color:#6B7280;">{foot}</td></tr>
</table></td></tr></table></body></html>"""


def _money(currency: str, amount) -> str:
    return f"{currency} {Decimal(str(amount)):,.2f}"


def _trip_dates(start, end=None) -> str:
    """'Wed 6 – Mon 11 January 2027' (canvas wording); one date if no end."""
    if not start:
        return ""
    if not end or end == start:
        return f"{start:%a} {start.day} {start:%B %Y}"
    if (start.month, start.year) == (end.month, end.year):
        return f"{start:%a} {start.day} – {end:%a} {end.day} {end:%B %Y}"
    return f"{start:%a} {start.day} {start:%B} – {end:%a} {end.day} {end:%B %Y}"


def _build_html(booking, payment) -> str:
    """Canvas: Emails · 'You're booked!'. Also the written booking contract
    (Terms Part A): what was booked and the exact policy versions accepted."""
    package = booking.package
    cur = booking.currency
    first_name = html.escape(booking.first_name)
    guests = booking.num_guests
    dates = _trip_dates(package.available_from, package.available_to) if booking.mode and package.available_from \
        else _trip_dates(booking.travel_date)

    from .email import build_claim_token  # self-import safe at call time
    claim = f"/claim?token={build_claim_token(booking)}"
    # Component bookings: one travel form per guest, reached after claiming.
    cta = _button(_url(claim + "&then=travel"), "Complete travel information") if booking.mode \
        else _button(_url(claim), "Open my booking")

    summary = [("Booking reference", f"<strong>{html.escape(booking.reference)}</strong>", ""),
               ("Lead traveller", f"{first_name} {html.escape(booking.last_name)}", ""),
               ("Total", _money(cur, booking.total_amount), "strong"),
               ("Paid", _money(cur, booking.amount_paid), "strong")]
    if booking.balance > 0:
        deadline = booking.effective_payment_deadline
        label = f"Balance due {deadline.day} {deadline:%B %Y}" if deadline else "Balance"
        summary.append((label, _money(cur, booking.balance), "strong"))

    from .receipts import compute_line_items
    items = compute_line_items(booking)
    lines = [(html.escape(l["label"]) + (f'<br><span style="font-size:11px;color:#6B7280;">{html.escape(l["detail"])}</span>' if l["detail"] else ""),
              _money(cur, l["amount"]), "") for l in items["lines"]]
    if items["bundle_discount"] > 0:
        lines.append((html.escape(items["discount_note"] or "Discount"), "− " + _money(cur, items["bundle_discount"]), "saving"))
    lines.append(("Total", _money(cur, items["total"]), "strong"))

    policy_paths = {"terms": "/terms-and-conditions", "refund": "/refund-policy",
                    "installment": "/installment-policy", "privacy": "/privacy-policy"}
    policy_links = " · ".join(
        f'<a href="{_url(policy_paths.get(a.document.type, "/terms-and-conditions"))}" style="{_LINK}">'
        f'{html.escape(a.document.title)} v{html.escape(a.document.version)}</a>'
        for a in booking.policy_acceptances.select_related("document")
    )

    # Privacy Policy: special requests can hold dietary/medical details, so
    # they are never echoed into email — the team confirms them privately.
    requests_note = _p("We've noted your special requests. Our team will confirm the arrangements with you directly.") \
        if booking.special_requests else ""

    body = (
        _h(f"See you in Ghana, {first_name}")
        + _p(f"Your booking for {html.escape(package.title)}, {dates}, is confirmed for "
             f"{guests} traveller{'s' if guests != 1 else ''}.")
        + _summary(summary)
        + cta
        + '<p style="margin:0 0 8px;font-size:11px;color:#6B7280;text-transform:uppercase;letter-spacing:1px;">What you booked</p>'
        + _summary(lines)
        + (_p(f"You accepted: {policy_links}. These versions form your booking contract.", small=True) if policy_links else "")
        + requests_note
    )
    footer = "Keep this email as your receipt."
    return _shell(f"You're booked! {booking.reference}", body, hero="booked.jpg", footer=footer)


def send_booking_confirmation(booking, payment) -> None:
    try:
        html_body = _build_html(booking, payment)
    except Exception:
        logger.exception("Could not build confirmation email for booking %s", booking.reference)
        return
    deliver("confirmation", booking.email, f"You're booked! {booking.reference} · {booking.package.title}",
            html_body, booking=booking)


def build_claim_token(booking) -> str:
    """Signed token that lets an authenticated account claim this booking on
    the dashboard. Possession of the emailed link is the proof of ownership —
    email-address match alone never grants access."""
    from django.core import signing

    return signing.dumps({"booking": str(booking.id)}, salt="booking-claim")


def _payment_summary_rows(booking, payment) -> list:
    cur = booking.currency
    rows = [("Payment received", _money(payment.currency, payment.amount), "strong"),
            ("Total booking amount", _money(cur, booking.total_amount), ""),
            ("Paid to date", _money(cur, booking.amount_paid), ""),
            ("Remaining balance", _money(cur, booking.balance), "strong")]
    deadline = booking.effective_payment_deadline
    if booking.balance > 0 and deadline:
        rows.append(("Final payment deadline", f"{deadline.day} {deadline:%B %Y}", ""))
    return rows


def send_payment_receipt(booking, payment) -> None:
    """
    Receipt for a successful payment that did not newly confirm the booking
    (installment top-ups, balance payments). Confirmation emails are sent by
    send_booking_confirmation on promotion only.
    """
    fully_paid = booking.is_paid
    if payment.purpose == "addon":
        added = next((l.get("name") for l in (booking.addons or []) if l.get("code") == payment.addon_code), "Your experience")
        heading = f"{html.escape(added)} added"
        sub = "It's paid for and now part of your booking. The updated details are below."
    else:
        heading = "Your booking is fully paid" if fully_paid else "Payment received"
        sub = ("Your booking is now fully paid. We can't wait to host you."
               if fully_paid else "Thank you. Your payment has been applied to your booking.")
    reference = payment.paystack_reference or f"offline:{str(payment.id)[:8]}"
    body = (_h(f"{heading}, {html.escape(booking.first_name)}") + _p(sub)
            + _summary([("Booking reference", f"<strong>{html.escape(booking.reference)}</strong>", ""),
                        ("Payment reference", html.escape(reference), "")] + _payment_summary_rows(booking, payment))
            + _button(_url("/dashboard"), "View my booking and payments"))
    deliver("receipt", booking.email, f"Payment receipt – {booking.reference} | Azura Travels",
            _shell(f"Receipt – {booking.reference}", body, footer="Keep this email as your receipt."), booking=booking)


# ── Cancellation request emails (Refund Policy, Part B) ──────────────────────

def _send(to: str, subject: str, html_body: str, what: str, booking=None) -> None:
    deliver(what.replace(" ", "_"), to, subject, html_body, booking=booking)


def send_cancellation_acknowledgment(req) -> None:
    """Dated acknowledgment — the timestamp here is the one the refund band is
    judged from, so the guest has it in writing immediately."""
    b = req.booking
    q = req.refund_quote or {}
    received = req.requested_at.strftime("%B %d, %Y at %H:%M GMT")
    body = (
        _h(f"We've received your cancellation request, {html.escape(b.first_name)}")
        + _p(f"Booking <strong>{b.reference}</strong> · request received <strong>{received}</strong>. "
             "Your refund entitlement is calculated from this moment, not from when we finish reviewing.")
        + _summary([("Amount paid", f'{b.currency} {q.get("net_paid", b.amount_paid)}', ""),
                    ("Days before departure", str(q.get("days_before_departure", "—")), ""),
                    ("Refund rate", f'{q.get("percent", "0")}%', ""),
                    ("Estimated refund", f'{b.currency} {q.get("refund_total", "0.00")}', "strong")])
        + _p("No further payments will be collected while your request is under review. "
             "We aim to review within five business days and to initiate any approved refund within ten business days of approval, "
             "to your original payment method. Nothing else changes on your booking until we confirm the outcome.")
    )
    _send(b.email, f"Cancellation request received – {b.reference} | Azura Travels",
          _shell("Cancellation request received", body), "cancellation acknowledgment", booking=b)


def send_cancellation_outcome(req) -> None:
    b = req.booking
    approved = req.status == "approved"
    q = req.refund_quote or {}
    note = _p(html.escape(req.staff_note)) if req.staff_note else ""
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
    body = _h(f"{heading}, {html.escape(b.first_name)}") + _p(body_text) + note
    _send(b.email, f"{heading} – {b.reference} | Azura Travels", _shell(heading, body), "cancellation outcome", booking=b)


def send_refund_processed(refund) -> None:
    """The refund has been initiated: amount, date, reference, where it goes."""
    b = refund.booking
    when = (refund.processed_at or timezone_now()).strftime("%B %d, %Y")
    via = "your original payment method" if refund.payment and refund.payment.method == "paystack" else "bank transfer"
    gateway = (refund.breakdown or {}).get("execute_on_gateway")
    rows = [("Amount", f"{refund.currency} {refund.amount}", "strong")]
    if gateway:
        rows.append(("Charged as", html.escape(gateway), ""))
    rows += [("Initiated", when, ""), ("Refund reference", html.escape(refund.external_reference or "—"), ""),
             ("Returned to", via, "")]
    body = (_h(f"Your refund is on its way, {html.escape(b.first_name)}")
            + _p(f"We've initiated a refund for booking <strong>{b.reference}</strong>.")
            + _summary(rows)
            + _p("Banks and card providers post refunds at different speeds, usually within 5–10 business days. "
                 "Quote the reference above if you need to ask us or your bank about it."))
    _send(b.email, f"Refund initiated – {b.reference} | Azura Travels", _shell("Refund initiated", body), "refund processed", booking=b)


def timezone_now():
    from django.utils import timezone
    return timezone.now()


def send_payment_failed(payment) -> None:
    """A card/MoMo attempt failed. Nothing was charged; here's the safe retry."""
    b = payment.booking
    body = (_h(f"A payment didn't go through, {html.escape(b.first_name)}")
            + _p(f"Your attempt to pay <strong>{payment.currency} {payment.amount}</strong> towards booking "
                 f"<strong>{b.reference}</strong> was declined by your bank or provider. <strong>Nothing was charged</strong>, "
                 "and your booking is unchanged.")
            + _p("You can try again whenever you're ready. A fresh, secure payment session is created each time.")
            + _button(_url(f"/booking/{b.reference}/pay"), "Try again")
            + _p("If this keeps happening, check with your bank.", small=True))
    deliver("payment_failed", b.email, f"Payment not completed – {b.reference} | Azura Travels",
            _shell("Payment not completed", body), booking=b)


def send_traveller_updated(booking, previous: dict, kind: str) -> None:
    """Terms Part A: confirm a name correction or traveller replacement in
    writing. The new traveller gets the booking link and a claim link for
    their own account; a replaced traveller's old address is told too."""
    b = booking
    status_url = _url(f"/payment/callback?reference={b.reference}&t={b.view_token}")
    claim_url = _url(f"/claim?token={build_claim_token(b)}")
    what = "name corrected" if kind == "correction" else "traveller updated"
    was = ("(previously " + html.escape(previous["first_name"] + " " + previous["last_name"]) + ")"
           if kind == "replacement" else "(spelling corrected)")
    body = (_h(f"Your booking details were updated, {html.escape(b.first_name)}")
            + _p(f"Booking <strong>{b.reference}</strong> · {html.escape(b.package.title)} now names "
                 f"<strong>{html.escape(b.first_name)} {html.escape(b.last_name)}</strong> as the traveller {was}. "
                 "All payments made stay with this booking. No charge applies for this change.")
            + _p(f'You can check the booking any time at <a href="{status_url}" style="{_LINK}">{status_url}</a>. '
                 "To manage payments and receipts in your own account, sign in or create an account with this email address.")
            + _button(claim_url, "Open my booking")
            + _p("Didn't expect this? Reply to this email immediately.", small=True))
    deliver(f"traveller_{kind}", b.email, f"Booking {what} – {b.reference} | Azura Travels", _shell("Booking updated", body), booking=b)

    if previous.get("email") and previous["email"].lower() != b.email.lower():
        old_body = (_h(f"Booking {b.reference} has been transferred")
                    + _p(f"At the request received by our team, booking <strong>{b.reference}</strong> now names a different traveller, "
                         "and this email address is no longer attached to it. If you did not ask for this, reply to this email immediately."))
        deliver("traveller_replaced_notice", previous["email"], f"Booking {b.reference} transferred | Azura Travels",
                _shell("Booking transferred", old_body), booking=b)


def send_addon_removed(booking, line: dict, fee, refund) -> None:
    b = booking
    name = html.escape(line.get("name", "an add-on"))
    charge = line.get("line_total", "0")
    outcome = (
        f"You'd already paid more than the new total, so <strong>{b.currency} {refund}</strong> is being refunded to your original payment method. We'll email the reference once it's initiated."
        if refund and Decimal(str(refund)) > 0 else
        f"Your remaining balance is now <strong>{b.currency} {b.balance}</strong>."
    )
    body = (_h(f"{name} has been removed, {html.escape(b.first_name)}")
            + _p(f"As requested, <strong>{name}</strong> ({b.currency} {charge}) has been cancelled on booking <strong>{b.reference}</strong>. "
                 f"Under the booking terms, 50% of a cancelled experience is retained: a cancellation fee of <strong>{b.currency} {fee}</strong> applies.")
            + _p(f"New booking total: <strong>{b.currency} {b.total_amount}</strong>. {outcome}"))
    deliver("addon_removed", b.email, f"{line.get('name', 'Add-on')} cancelled – {b.reference} | Azura Travels",
            _shell("Add-on cancelled", body), booking=b)


def send_saved_quote(lead, reminder: bool = False) -> bool:
    """Canvas: Emails · 'Your Ghana trip is saved'. The guest saved a
    trip-builder quote (or left checkout): send it back with a link to resume.
    Prices are the server quote stored on the lead, never client figures.
    Reminders carry an unsubscribe link."""
    from packages.models import TravelPackage

    q = lead.quote or {}
    if not q:
        return False
    c = q.get("currency", "USD")
    rows = [("Core experience × " + str(q.get("num_guests", 1)), _money(c, q.get("core_total", "0")), "")]
    for key, label in (("rooms_total", "Accra stay"), ("experiences_total", "Experiences"), ("transfers_total", "Airport transfers")):
        if Decimal(q.get(key, "0")):
            rows.append((label, _money(c, q[key]), ""))
    if Decimal(q.get("discount_total", "0")):
        rows.append((f'Includes {html.escape(q.get("discount_name") or "group saving")} (accommodation)',
                     "−" + _money(c, q["discount_total"]), "saving"))
    rows += [("Total", _money(c, q.get("total", "0")), "strong"),
             ("Average per person", _money(c, q.get("average_per_person", "0")), "")]

    package = TravelPackage.objects.filter(pk=q.get("package_id") or None).first()
    trip = (f"{html.escape(package.title)}, {_trip_dates(package.available_from, package.available_to)}"
            if package else "your Ghana trip")
    body = (_h("Pick up where you left off" if reminder else f"Your Ghana trip is saved, {html.escape(lead.name)}")
            + _p(f"Here's the quote you saved for {trip}.")
            + _summary(rows)
            + _button(_url(f"/tour/{q.get('package_id', '')}/build"), "Resume my booking")
            + _p("Prices and room availability can change until you book. Rooms are held only during checkout.", small=True))
    footer = ""
    if reminder:
        from bookings.crm import unsubscribe_url
        footer = (f"You're receiving this because you asked us to keep you updated. "
                  f'<a href="{unsubscribe_url(lead)}" style="color:#6B7280;">Unsubscribe</a>. We stop these emails once you book.')
    subject = "Your Ghana trip is still waiting | Azura Travels" if reminder else "Your Ghana trip is saved | Azura Travels"
    return deliver("quote_reminder" if reminder else "saved_quote", lead.email, subject,
                   _shell("Your Ghana trip is saved", body, hero="saved.jpg", footer=footer), lead=lead)


def send_donation_receipt(donation) -> None:
    """Thank-you and receipt for an Academy donation (separate from any booking receipt)."""
    from django.utils import timezone

    body = (_h(f"Thank you, {html.escape(donation.donor_name)}")
            + _p("Your donation to the Michael Blackson Academy has been received.")
            + _summary([("Donation", _money(donation.currency, donation.amount), "strong"),
                        ("Charged", _money(donation.charged_currency, donation.charged_amount), ""),
                        ("Reference", html.escape(donation.reference), ""),
                        ("Date", f"{(donation.paid_at or timezone.now()):%d %b %Y}", "")])
            + _p("This donation is voluntary, separate from any travel booking, and goes to the Academy's own account.", small=True))
    if deliver("donation_receipt", donation.donor_email, "Thank you for supporting the Academy | Azura Travels",
               _shell("Donation receipt", body), booking=donation.booking):
        type(donation).objects.filter(pk=donation.pk).update(receipt_sent_at=timezone.now())
