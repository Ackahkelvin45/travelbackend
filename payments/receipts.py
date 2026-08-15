"""
Payment breakdown + printable receipt.

The gateway charge is a single lump sum, but a booking is composed of known
line items captured in the commercial snapshot at booking time:

    total_amount = base_total (package/hotel option × guests)
                 + add-ons  (e.g. Visa on Arrival, per guest)

`early_bird_discount` is the saving already baked into the effective price
(standard would have been base_total + discount) — shown for transparency,
never re-subtracted. Everything here is derived from stored booking fields, so
the receipt always reconciles with the ledger; no numbers are invented.
"""
from decimal import Decimal
import html as _html

from django.utils import timezone

from .money import quantize

# What Azura sells today: a package built from a hotel/occupancy OPTION, plus
# optional per-guest add-ons (visa). There are no separate flight/ticket/tax/
# commission/platform-fee concepts in the data model — the breakdown reflects
# exactly what exists, nothing more.


def compute_line_items(booking) -> dict:
    """Decompose a booking into its priced components.

    Returns a dict with `lines` (each: label, detail, amount), the discount,
    the currency, and reconciliation totals. `reconciles` is True when the
    component sum equals the stored booking total — a guard the admin surfaces
    so a snapshot drift can never masquerade as a balanced receipt.
    """
    currency = booking.currency
    guests = booking.num_guests or 1
    base_total = quantize(Decimal(booking.unit_price) * guests)

    snap = booking.option_snapshot or {}
    hotel = snap.get("hotel_name")
    occupancy = snap.get("occupancy_display") or snap.get("occupancy")
    if hotel:
        pkg_detail = f"{booking.package.title} — {hotel}"
        if occupancy:
            pkg_detail += f" ({occupancy})"
    else:
        pkg_detail = booking.package.title
    pkg_detail += f" · {guests} guest{'s' if guests != 1 else ''} × {currency} {Decimal(booking.unit_price):,.2f}"

    lines = [{"label": "Package / Tour", "detail": pkg_detail, "amount": base_total, "category": "package"}]

    addons_total = Decimal("0.00")
    for addon in booking.addons or []:
        line_total = quantize(Decimal(str(addon.get("line_total", "0"))))
        addons_total += line_total
        qty = addon.get("quantity", 1)
        unit = addon.get("unit_price", "0")
        lines.append({
            "label": addon.get("name", addon.get("code", "Add-on")),
            "detail": f"{qty} × {currency} {Decimal(str(unit)):,.2f}"
                      + ("" if addon.get("refundable", True) else " · non-refundable"),
            "amount": line_total,
            "category": addon.get("code", "addon"),
        })

    discount = quantize(Decimal(booking.early_bird_discount or 0))
    component_sum = quantize(base_total + addons_total)
    total = quantize(Decimal(booking.total_amount))

    return {
        "currency": currency,
        "lines": lines,
        "base_total": base_total,
        "addons_total": addons_total,
        "discount": discount,
        "component_sum": component_sum,
        "total": total,
        "reconciles": component_sum == total,
        "amount_paid": quantize(Decimal(booking.amount_paid)),
        "amount_refunded": quantize(Decimal(booking.amount_refunded)),
        "balance": quantize(Decimal(booking.balance)),
    }


def receipt_number(payment) -> str:
    """Stable, human receipt id derived from the payment reference."""
    ref = payment.paystack_reference or f"OFFLINE-{str(payment.id)[:8].upper()}"
    return f"AZT-RCPT-{ref}"


def render_receipt_html(payment, *, business_name="Azura Travels",
                        support_email="hello@azuratravels.live") -> str:
    """Self-contained printable receipt (browser Save-as-PDF / print).

    Only for a payment that actually moved money (SUCCESS); the caller enforces
    that so a 'paid' receipt is never produced for a pending/failed attempt.
    """
    booking = payment.booking
    bd = compute_line_items(booking)
    e = _html.escape  # user-controlled fields must never break the HTML

    def money(amount, currency=None):
        return f"{currency or bd['currency']} {Decimal(amount):,.2f}"

    line_rows = "".join(
        f"<tr><td style='padding:10px 0;border-bottom:1px solid #eee;'>"
        f"<div style='font-weight:600;color:#1a1a2e;'>{e(l['label'])}</div>"
        f"<div style='color:#777;font-size:12px;'>{e(l['detail'])}</div></td>"
        f"<td style='padding:10px 0;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;'>{money(l['amount'])}</td></tr>"
        for l in bd["lines"]
    )
    discount_row = (
        f"<tr><td style='padding:8px 0;color:#0a7d3f;'>Early-bird saving applied</td>"
        f"<td style='padding:8px 0;text-align:right;color:#0a7d3f;'>− {money(bd['discount'])}</td></tr>"
        if bd["discount"] > 0 else ""
    )
    refund_row = (
        f"<tr><td style='padding:8px 0;color:#b3261e;'>Refunded</td>"
        f"<td style='padding:8px 0;text-align:right;color:#b3261e;'>− {money(bd['amount_refunded'])}</td></tr>"
        if bd["amount_refunded"] > 0 else ""
    )
    reconcile_warn = (
        "" if bd["reconciles"] else
        "<p style='margin:12px 0 0;padding:10px 14px;background:#fef2f2;color:#991b1b;border-radius:8px;"
        "font-size:12px;'>⚠ Components do not sum to the booking total — snapshot needs review.</p>"
    )

    charged = ""
    if payment.charged_amount and payment.charged_currency and payment.charged_currency != payment.currency:
        charged = (f"<tr><td style='padding:4px 0;color:#777;'>Charged to gateway</td>"
                   f"<td style='padding:4px 0;text-align:right;'>{money(payment.charged_amount, payment.charged_currency)} "
                   f"<span style='color:#999;'>@ {payment.exchange_rate}</span></td></tr>")

    paid_at = payment.paid_at.strftime("%B %d, %Y · %H:%M") if payment.paid_at else "—"
    generated = timezone.now().strftime("%B %d, %Y · %H:%M")
    customer = e(f"{booking.first_name} {booking.last_name}".strip())

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Receipt {e(receipt_number(payment))}</title>
<style>
  @media print {{ .no-print {{ display:none !important; }} body {{ background:#fff !important; }} }}
  body {{ font-family:'Helvetica Neue',Helvetica,Arial,sans-serif; background:#f4f2ec; margin:0; padding:32px; color:#1a1a2e; }}
  .sheet {{ max-width:720px; margin:0 auto; background:#fff; border-radius:14px; padding:40px; box-shadow:0 2px 20px rgba(0,0,0,.06); }}
  h1 {{ font-size:22px; letter-spacing:3px; text-transform:uppercase; color:#bd8f3a; margin:0; }}
  table {{ width:100%; border-collapse:collapse; }}
  .meta td {{ padding:3px 0; font-size:13px; vertical-align:top; }}
  .meta td:first-child {{ color:#777; padding-right:18px; white-space:nowrap; }}
  .btn {{ display:inline-block; padding:11px 22px; background:#1a1a2e; color:#fff; border:none;
          border-radius:999px; font-weight:700; font-size:13px; cursor:pointer; text-decoration:none; }}
</style></head>
<body>
  <div class="no-print" style="max-width:720px;margin:0 auto 16px;text-align:right;">
    <button class="btn" onclick="window.print()">⬇ Download / Print receipt</button>
  </div>
  <div class="sheet">
    <table style="margin-bottom:26px;"><tr>
      <td><h1>{e(business_name)}</h1>
          <div style="color:#777;font-size:12px;margin-top:4px;">Premium Travel Experiences</div></td>
      <td style="text-align:right;">
        <div style="font-size:12px;color:#777;text-transform:uppercase;letter-spacing:1px;">Receipt</div>
        <div style="font-weight:700;font-size:14px;">{e(receipt_number(payment))}</div>
        <div style="font-size:12px;color:#777;margin-top:4px;">Issued {generated}</div>
      </td>
    </tr></table>

    <table style="margin-bottom:24px;"><tr>
      <td style="vertical-align:top;width:50%;">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">Billed to</div>
        <table class="meta">
          <tr><td>Name</td><td style="color:#1a1a2e;font-weight:600;">{customer}</td></tr>
          <tr><td>Email</td><td>{e(booking.email)}</td></tr>
          {f"<tr><td>Phone</td><td>{e(booking.phone)}</td></tr>" if booking.phone else ""}
          {f"<tr><td>Country</td><td>{e(booking.country)}</td></tr>" if booking.country else ""}
        </table>
      </td>
      <td style="vertical-align:top;width:50%;">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">Payment</div>
        <table class="meta">
          <tr><td>Booking ref</td><td style="font-weight:600;">{e(booking.reference)}</td></tr>
          <tr><td>Payment ref</td><td>{e(payment.paystack_reference or "—")}</td></tr>
          <tr><td>Status</td><td style="font-weight:700;text-transform:uppercase;">{e(payment.get_status_display())}</td></tr>
          <tr><td>Method</td><td>{e(payment.get_method_display())}</td></tr>
          <tr><td>Paid at</td><td>{paid_at}</td></tr>
        </table>
      </td>
    </tr></table>

    <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">What this paid for</div>
    <table>{line_rows}</table>

    <table style="margin-top:14px;border-top:2px solid #1a1a2e;">
      <tr><td style="padding:10px 0 4px;color:#777;">Subtotal</td>
          <td style="padding:10px 0 4px;text-align:right;">{money(bd['component_sum'])}</td></tr>
      {discount_row}
      <tr><td style="padding:4px 0;font-weight:800;font-size:16px;">Booking total</td>
          <td style="padding:4px 0;text-align:right;font-weight:800;font-size:16px;">{money(bd['total'])}</td></tr>
      <tr><td style="padding:4px 0;color:#0a7d3f;">Paid to date</td>
          <td style="padding:4px 0;text-align:right;color:#0a7d3f;font-weight:600;">{money(bd['amount_paid'])}</td></tr>
      {refund_row}
      <tr><td style="padding:4px 0;color:#777;">Balance</td>
          <td style="padding:4px 0;text-align:right;">{money(bd['balance'])}</td></tr>
      {charged}
    </table>
    {reconcile_warn}

    <p style="margin:28px 0 0;color:#999;font-size:12px;border-top:1px solid #eee;padding-top:16px;">
      This receipt reflects the payment ledger for booking {e(booking.reference)}.
      Questions? Contact {e(support_email)}. Thank you for choosing {e(business_name)}.
    </p>
  </div>
</body></html>"""
