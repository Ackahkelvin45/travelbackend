"""
bookings/pricing.py
-------------------
The single source of truth for what an option-based booking costs.

Every number the customer sees comes from here (via the pricing-matrix
endpoint), and booking creation re-runs the same computation server-side and
snapshots the result — the frontend only ever *displays* prices, it never
computes them.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from payments.money import quantize


class QuoteError(Exception):
    """Raised when a selection cannot be priced (inactive option, bad plan…)."""


@dataclass
class Quote:
    option: object
    package: object
    num_guests: int
    payment_plan: str

    # per-person and totals (all quantized Decimals)
    standard_price_per_person: Decimal = Decimal("0")
    effective_price_per_person: Decimal = Decimal("0")
    early_bird_applied: bool = False
    base_total: Decimal = Decimal("0")
    early_bird_discount: Decimal = Decimal("0")  # total saving vs standard
    addons: list = field(default_factory=list)
    addons_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    deposit_required: Decimal | None = None
    amount_due_today: Decimal = Decimal("0")

    def as_dict(self):
        return {
            "option_id": str(self.option.id),
            "hotel_name": self.option.hotel_name,
            "star_rating": self.option.star_rating,
            "occupancy": self.option.occupancy,
            "num_guests": self.num_guests,
            "payment_plan": self.payment_plan,
            "currency": self.package.currency,
            "standard_price_per_person": str(self.standard_price_per_person),
            "effective_price_per_person": str(self.effective_price_per_person),
            "early_bird_applied": self.early_bird_applied,
            "base_total": str(self.base_total),
            "early_bird_discount": str(self.early_bird_discount),
            "addons": self.addons,
            "addons_total": str(self.addons_total),
            "total": str(self.total),
            "deposit_required": str(self.deposit_required) if self.deposit_required is not None else None,
            "amount_due_today": str(self.amount_due_today),
            "final_payment_deadline": self.package.final_payment_deadline,
            "early_bird_deadline": self.package.early_bird_deadline,
        }


def compute_quote(option, *, visa: bool, payment_plan: str, at=None) -> Quote:
    """
    Price one selection at one moment in time.

    Early-bird eligibility is decided by ``at`` (booking creation time when
    called from checkout) — a customer who books before the deadline keeps the
    price even if the charge lands after it.
    """
    at = at or timezone.now()
    package = option.package

    if not package.is_active:
        raise QuoteError("This tour is not open for booking.")
    if not option.is_active:
        raise QuoteError("This room option is no longer available.")

    from bookings.models import Booking  # local import to avoid cycles

    if payment_plan not in (Booking.PaymentPlan.FULL, Booking.PaymentPlan.INSTALLMENT):
        raise QuoteError("Unknown payment plan.")
    if payment_plan == Booking.PaymentPlan.INSTALLMENT and not package.allow_installments:
        raise QuoteError("Installment payment is not available for this tour.")

    num_guests = option.guests_per_booking
    effective_pp, early_bird_applied = option.effective_price(at=at)
    standard_pp = option.price_per_person

    quote = Quote(
        option=option,
        package=package,
        num_guests=num_guests,
        payment_plan=payment_plan,
        standard_price_per_person=quantize(standard_pp),
        effective_price_per_person=quantize(effective_pp),
        early_bird_applied=early_bird_applied,
    )
    quote.base_total = quantize(effective_pp * num_guests)
    quote.early_bird_discount = (
        quantize((standard_pp - effective_pp) * num_guests) if early_bird_applied else Decimal("0.00")
    )

    # ── Add-ons ──────────────────────────────────────────────────────────────
    # Visa is per guest and non-refundable (third-party cost) — the snapshot
    # carries the flag so refunds exclude it even if config changes later.
    if visa:
        if not (package.visa_addon_enabled and package.visa_fee):
            raise QuoteError("The visa service is not available for this tour.")
        line_total = quantize(package.visa_fee * num_guests)
        quote.addons.append({
            "code": "visa",
            "name": "Visa on Arrival",
            "unit_price": str(quantize(package.visa_fee)),
            "quantity": num_guests,
            "line_total": str(line_total),
            "refundable": False,
        })
        quote.addons_total = line_total

    quote.total = quantize(quote.base_total + quote.addons_total)

    # ── Payment plan ─────────────────────────────────────────────────────────
    if payment_plan == Booking.PaymentPlan.INSTALLMENT:
        if not package.deposit_minimum:
            raise QuoteError("Installment payment is not configured for this tour.")
        quote.deposit_required = quantize(min(package.deposit_minimum, quote.total))
        quote.amount_due_today = quote.deposit_required
    else:
        quote.amount_due_today = quote.total

    return quote


def _charge_info(package) -> dict:
    if package.currency == "GHS":
        return {"currency": "GHS", "exchange_rate": None, "rate_source": None}
    from payments.fx import FxUnavailable, effective_charge_rate

    try:
        rate, source = effective_charge_rate(package)
        return {"currency": "GHS", "exchange_rate": str(rate), "rate_source": source}
    except FxUnavailable:
        # Display degrades gracefully; initialize will 503 until a rate exists.
        return {"currency": "GHS", "exchange_rate": None, "rate_source": None}


def build_pricing_matrix(package, at=None) -> dict:
    """
    The full price space of a package in one response — every option priced
    both ways, so the frontend's selection UI is pure lookup with zero
    client-side money math and zero per-click round-trips.
    """
    at = at or timezone.now()
    early_bird_active = bool(package.early_bird_deadline and at <= package.early_bird_deadline)

    options = []
    for option in package.options.filter(is_active=True):
        effective_pp, eb_applied = option.effective_price(at=at)
        guests = option.guests_per_booking
        options.append({
            "id": str(option.id),
            "hotel_name": option.hotel_name,
            "star_rating": option.star_rating,
            "hotel_image": option.hotel_image.url if option.hotel_image else None,
            "occupancy": option.occupancy,
            "occupancy_display": option.get_occupancy_display(),
            "guests_per_booking": guests,
            "standard_price_per_person": str(quantize(option.price_per_person)),
            "early_bird_price_per_person": (
                str(quantize(option.early_bird_price_per_person))
                if option.early_bird_price_per_person is not None else None
            ),
            "effective_price_per_person": str(quantize(effective_pp)),
            "early_bird_applied": eb_applied,
            "standard_total": str(quantize(option.price_per_person * guests)),
            "effective_total": str(quantize(effective_pp * guests)),
            "saving_total": str(quantize((option.price_per_person - effective_pp) * guests)),
        })

    return {
        "package_id": str(package.id),
        "currency": package.currency,
        "tour_start": package.available_from,
        "tour_end": package.available_to,
        "options": options,
        "visa": {
            "enabled": bool(package.visa_addon_enabled and package.visa_fee),
            "fee_per_guest": str(quantize(package.visa_fee)) if package.visa_fee else None,
            "info": package.visa_info,
            "refundable": False,
        },
        "installments": {
            "enabled": bool(package.allow_installments and package.deposit_minimum),
            "deposit_minimum": str(quantize(package.deposit_minimum)) if package.deposit_minimum else None,
            "final_payment_deadline": package.final_payment_deadline,
        },
        "early_bird": {
            "active": early_bird_active,
            "deadline": package.early_bird_deadline,
        },
        # Paystack Ghana charges GHS only: non-GHS packages disclose the
        # conversion applied at charge time ("You'll be charged GHS X"),
        # using the SAME resolution as the charge itself (live rate + margin,
        # falling back per payments.fx) so display always matches the charge.
        "charge": _charge_info(package),
        # Server clock — the frontend corrects browser clock drift with
        # offset = server_now - Date.now() and derives countdowns from it.
        "server_now": at,
    }
