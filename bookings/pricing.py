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


def _fee_notice() -> str:
    from payments.models import OpsConfig
    return OpsConfig.get().checkout_fee_notice


def assert_capacity(package, num_guests: int) -> None:
    """Refuse a checkout that would exceed the tour's total capacity."""
    left = package.spots_left
    if left is None:
        return
    if num_guests > left:
        raise QuoteError(
            "This tour is fully booked." if left == 0
            else f"Only {left} place{'s' if left != 1 else ''} left on this tour."
        )


def installments_open(package, today=None) -> bool:
    """Deposits are only offered BEFORE the final payment deadline — a booking
    made on or after that date is payable in full (policy Part C). Ghana keeps
    GMT year-round and settings.TIME_ZONE is UTC, so localdate() IS the
    Africa/Accra date the policy's 23:59 boundary is expressed in."""
    if not (package.allow_installments and package.deposit_minimum):
        return False
    if package.final_payment_deadline:
        from django.utils import timezone
        return (today or timezone.localdate()) < package.final_payment_deadline
    return True


def deposit_for(package, total: Decimal, num_guests: int) -> Decimal:
    """The minimum initial payment for this cart.

    per_booking  → one minimum for the whole booking (capped at the total).
    per_traveller → the minimum for each guest, each capped at that guest's
    share of the total, then summed: two $878 travellers owe $1,756, not
    $1,000, while a lone $878 traveller owes $878 and never the $1,000 minimum.
    """
    from packages.models import TravelPackage

    minimum = package.deposit_minimum
    if package.deposit_unit == TravelPackage.DepositUnit.PER_TRAVELLER and num_guests > 0:
        share = quantize(total / num_guests)
        return quantize(min(minimum, share) * num_guests)
    return quantize(min(minimum, total))


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
        if not installments_open(package):
            raise QuoteError(
                "The final payment deadline has passed — this booking is payable in full."
            )
        quote.deposit_required = deposit_for(package, quote.total, num_guests)
        quote.amount_due_today = quote.deposit_required
    else:
        quote.amount_due_today = quote.total

    return quote


@dataclass
class ConfigurableQuote:
    """A quote for a core_plus_addons package: mandatory base + chosen add-ons,
    with the single highest qualifying bundle discount applied to the subtotal."""
    package: object
    num_guests: int
    payment_plan: str
    base_price_per_person: Decimal
    base_total: Decimal
    addons: list = field(default_factory=list)
    addons_total: Decimal = Decimal("0")
    subtotal: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    discount_note: str = ""
    total: Decimal = Decimal("0")
    deposit_required: Decimal | None = None
    amount_due_today: Decimal = Decimal("0")

    def as_dict(self):
        return {
            "package_id": str(self.package.id),
            "pricing_model": "core_plus_addons",
            "num_guests": self.num_guests,
            "payment_plan": self.payment_plan,
            "currency": self.package.currency,
            "base_price_per_person": str(self.base_price_per_person),
            "base_total": str(self.base_total),
            "addons": self.addons,
            "addons_total": str(self.addons_total),
            "subtotal": str(self.subtotal),
            "discount_percent": str(self.discount_percent),
            "discount_amount": str(self.discount_amount),
            "discount_note": self.discount_note,
            "total": str(self.total),
            "deposit_required": str(self.deposit_required) if self.deposit_required is not None else None,
            "amount_due_today": str(self.amount_due_today),
        }


def compute_configurable_quote(package, *, num_guests: int, selected_addon_codes, payment_plan) -> ConfigurableQuote:
    """Price a core_plus_addons selection server-side.

    base_total = base_price_per_person × guests; each add-on is per-person
    (× guests) or flat; the discount is the single highest rule whose required
    add-ons are all selected (never stacks), applied to the subtotal.
    """
    from bookings.models import Booking
    from packages.models import TravelPackage

    if not package.is_active:
        raise QuoteError("This tour is not open for booking.")
    if package.pricing_model != TravelPackage.PricingModel.CORE_PLUS_ADDONS:
        raise QuoteError("This tour does not use the core + add-ons pricing model.")
    if package.base_price_per_person is None:
        raise QuoteError("This tour has no base price configured.")
    if not isinstance(num_guests, int) or num_guests < 1:
        raise QuoteError("At least one guest is required.")
    if payment_plan not in (Booking.PaymentPlan.FULL, Booking.PaymentPlan.INSTALLMENT):
        raise QuoteError("Unknown payment plan.")
    if payment_plan == Booking.PaymentPlan.INSTALLMENT and not package.allow_installments:
        raise QuoteError("Installment payment is not available for this tour.")

    base_pp = package.base_price_per_person
    base_total = quantize(base_pp * num_guests)

    active = {a.code: a for a in package.addons.filter(is_active=True)}
    selected = set(selected_addon_codes or [])
    unknown = selected - set(active)
    if unknown:
        raise QuoteError(f"Unknown add-on(s): {', '.join(sorted(unknown))}.")

    # Group rules: single-select groups accept at most one; auto-apply a $0
    # default (e.g. 'No Hotel') when nothing is chosen; enforce required groups.
    by_group = {}
    for code in selected:
        a = active[code]
        if a.group_id:
            by_group.setdefault(a.group_id, []).append(a)
    for group in package.addon_groups.all():
        chosen = by_group.get(group.id, [])
        if group.selection == group.Selection.SINGLE and len(chosen) > 1:
            raise QuoteError(f"Please choose only one option in '{group.name}'.")
        if group.selection == group.Selection.SINGLE and not chosen:
            default = next((a for a in active.values() if a.group_id == group.id and a.is_default), None)
            if default:
                selected.add(default.code)
            elif group.required:
                raise QuoteError(f"Please choose an option in '{group.name}'.")

    addons, addons_total = [], Decimal("0")
    for code in sorted(selected, key=lambda c: (active[c].order, active[c].name)):
        a = active[code]
        qty = num_guests if a.unit == a.Unit.PER_PERSON else 1
        line_total = quantize(a.price * qty)
        addons_total += line_total
        addons.append({
            "code": a.code, "name": a.name, "unit": a.unit,
            "unit_price": str(quantize(a.price)), "quantity": qty,
            "line_total": str(line_total), "refundable": a.refundable,
        })

    subtotal = quantize(base_total + addons_total)

    best_percent, note = Decimal("0"), ""
    for rule in package.discount_rules.filter(is_active=True).prefetch_related("required_addons"):
        req = {a.code for a in rule.required_addons.all()}
        if req and req <= selected and rule.percent > best_percent:
            best_percent, note = rule.percent, rule.name
    discount_amount = quantize(subtotal * best_percent / Decimal("100"))
    total = quantize(subtotal - discount_amount)

    quote = ConfigurableQuote(
        package=package, num_guests=num_guests, payment_plan=payment_plan,
        base_price_per_person=quantize(base_pp), base_total=base_total,
        addons=addons, addons_total=quantize(addons_total), subtotal=subtotal,
        discount_percent=best_percent, discount_amount=discount_amount,
        discount_note=note, total=total,
    )
    if payment_plan == Booking.PaymentPlan.INSTALLMENT:
        if not package.deposit_minimum:
            raise QuoteError("Installment payment is not configured for this tour.")
        if not installments_open(package):
            raise QuoteError(
                "The final payment deadline has passed — this booking is payable in full."
            )
        quote.deposit_required = deposit_for(package, total, num_guests)
        quote.amount_due_today = quote.deposit_required
    else:
        quote.amount_due_today = total
    return quote


def build_configurable_matrix(package) -> dict:
    """Everything the cart UI needs for a core_plus_addons package: base price,
    add-on groups + add-ons, and the discount rules (for transparency)."""
    groups = []
    for g in package.addon_groups.all():
        groups.append({
            "id": str(g.id), "name": g.name, "selection": g.selection, "required": g.required,
            "addons": [
                {
                    "code": a.code, "name": a.name, "description": a.description,
                    "price": str(quantize(a.price)), "unit": a.unit,
                    "is_default": a.is_default, "refundable": a.refundable,
                }
                for a in package.addons.filter(is_active=True, group=g)
            ],
        })
    ungrouped = [
        {
            "code": a.code, "name": a.name, "description": a.description,
            "price": str(quantize(a.price)), "unit": a.unit,
            "is_default": a.is_default, "refundable": a.refundable,
        }
        for a in package.addons.filter(is_active=True, group__isnull=True)
    ]
    rules = [
        {"name": r.name, "percent": str(r.percent),
         "required_addons": [a.code for a in r.required_addons.all()]}
        for r in package.discount_rules.filter(is_active=True)
    ]
    return {
        "package_id": str(package.id),
        "pricing_model": "core_plus_addons",
        "currency": package.currency,
        "base_price_per_person": str(quantize(package.base_price_per_person)) if package.base_price_per_person else None,
        "tour_start": package.available_from,
        "tour_end": package.available_to,
        "addon_groups": groups,
        "ungrouped_addons": ungrouped,
        "discount_rules": rules,
        "installments": {
            "enabled": installments_open(package),
            "deposit_minimum": str(quantize(package.deposit_minimum)) if package.deposit_minimum else None,
            "deposit_unit": package.deposit_unit,
            "final_payment_deadline": package.final_payment_deadline,
        },
        "charge": _charge_info(package),
        "notices": {"fees": _fee_notice()},
    }


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
            "enabled": installments_open(package),
            "deposit_minimum": str(quantize(package.deposit_minimum)) if package.deposit_minimum else None,
            "deposit_unit": package.deposit_unit,
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
        "notices": {"fees": _fee_notice()},
        # Server clock — the frontend corrects browser clock drift with
        # offset = server_now - Date.now() and derives countdowns from it.
        "server_now": at,
    }
