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
    from packages.models import TravelPackage

    if not package.allow_installments:
        return False
    # Component bookings split by accommodation_deposit_percent, not a minimum.
    if package.pricing_model != TravelPackage.PricingModel.COMPONENT and not package.deposit_minimum:
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


# ── Component pricing (Change Request v4) ─────────────────────────────────────

def catalog_snapshot(package, *, everything_on_sale: bool = False) -> dict:
    """The editable catalogue tables (the draft) as JSON-safe data.
    everything_on_sale prices unconfirmed rooms too (acceptance checks only)."""
    from packages.models import PackageAddon, PackageDiscountRule

    return {
        "core_price": str(package.base_price_per_person) if package.base_price_per_person is not None else None,
        "stay_types": [
            {"code": s.code, "name": s.name, "kind": s.kind, "capacity": s.capacity, "min_occupancy": s.min_occupancy,
             "price_per_night": str(s.price_per_night), "nights": s.nights,
             "on_sale": s.on_sale or everything_on_sale, "note": s.note,
             "stay_only": s.stay_only, "check_in": s.check_in.isoformat() if s.check_in else None}
            for s in package.stay_types.order_by("kind", "order")  # Airbnb first, then hotel
        ],
        "experiences": [
            {"code": a.code, "name": a.name, "description": a.description or "", "price": str(a.price),
             "refundable": a.refundable, "unit": a.unit, "adults_only": a.adults_only,
             "requires_stay": a.requires_stay, "vehicle_capacity": a.vehicle_capacity}
            for a in package.addons.filter(
                is_active=True, unit__in=[PackageAddon.Unit.PER_PERSON, PackageAddon.Unit.PER_VEHICLE],
            ).order_by("order", "name")
        ],
        "group_discounts": [
            {"name": r.name, "percent": str(r.percent), "min_travellers": r.min_travellers}
            for r in package.discount_rules.filter(
                scope=PackageDiscountRule.Scope.GROUP_ACCOMMODATION, is_active=True, min_travellers__isnull=False,
            )
        ],
    }


@dataclass
class Catalog:
    version: object  # PriceListVersion, or None while nothing is published (prices from the draft tables)
    core_price: Decimal | None
    stay_types: dict
    experiences: dict
    group_discounts: list


_STAY_DEFAULTS = {"stay_only": False, "check_in": None}
_EXPERIENCE_DEFAULTS = {"unit": "per_person", "adults_only": False, "requires_stay": False, "vehicle_capacity": 2}


def _catalog(snap: dict, version) -> Catalog:
    from types import SimpleNamespace as NS
    dec = lambda d, *keys: {**d, **{k: Decimal(d[k]) for k in keys}}
    return Catalog(
        version=version,
        core_price=Decimal(snap["core_price"]) if snap.get("core_price") is not None else None,
        stay_types={s["code"]: NS(**{**_STAY_DEFAULTS, **dec(s, "price_per_night")}) for s in snap["stay_types"]},
        # Snapshots published before v4 lack the newer keys.
        experiences={e["code"]: NS(**{**_EXPERIENCE_DEFAULTS, **dec(e, "price")}) for e in snap["experiences"]},
        group_discounts=[(Decimal(g["percent"]), g["name"], g["min_travellers"]) for g in snap.get("group_discounts", [])],
    )


def live_catalog(package) -> Catalog:
    """Prices customers see: the live published version, or the draft tables
    for a package that has never been published."""
    from packages.models import PriceListVersion
    version = package.price_lists.filter(status=PriceListVersion.Status.LIVE).order_by("-number").first()
    return _catalog(version.snapshot if version else catalog_snapshot(package), version)


def draft_catalog(package) -> Catalog:
    return _catalog(catalog_snapshot(package), None)


# The client's acceptance totals (Change Request v4 §5 and §9, Core $999 from
# 27 Oct 2026), before any
# group discount — (label, mode, travellers' experiences, rooms as
# (stay code, occupants), expected, compare) where compare is "total" or
# "extras" (total minus Core).
ACCEPTANCE_SCENARIOS = [
    ("Couple · Core only", "couple", [[], []], [], "1998.00", "total"),
    ("Couple · one-bedroom Airbnb, 5 nights", "couple", [[], []], [("airbnb_1bed", [0, 1])], "3098.00", "total"),
    ("Couple · two-bedroom Airbnb, 5 nights", "couple", [[], []], [("airbnb_2bed", [0, 1])], "3698.00", "total"),
    ("Couple · Four Points couple room, 4 nights", "couple", [[], []], [("fourpoints_couple", [0, 1])], "3518.00", "total"),
    ("Single · one-bedroom Airbnb", "single", [[]], [("airbnb_1bed", [0])], "2099.00", "total"),
    ("Single · Four Points standard room", "single", [[]], [("fourpoints_standard", [0])], "2319.00", "total"),
    ("Two-person ENZO VIP", "couple", [["enzo_vip"], ["enzo_vip"]], [], "440.00", "extras"),
]


def acceptance_checks(package) -> list:
    """Price the client's acceptance scenarios against the DRAFT catalogue with
    every room treated as on sale and no group discount. [{label, expected, got, ok}]."""
    snap = catalog_snapshot(package, everything_on_sale=True)
    cat = _catalog({**snap, "group_discounts": []}, None)
    results = []
    for label, mode, exps, rooms, expected, compare in ACCEPTANCE_SCENARIOS:
        try:
            q = compute_component_quote(
                package, mode=mode, travellers=[{"experiences": e, "adult": True} for e in exps],
                rooms=[{"stay_type": c, "occupants": o} for c, o in rooms],
                payment_plan="full", check_availability=False, catalog=cat,
            )
            got = q.total - q.core_total if compare == "extras" else q.total
            results.append({"label": label, "expected": expected, "got": f"{got:.2f}", "ok": f"{got:.2f}" == expected})
        except QuoteError as exc:
            results.append({"label": label, "expected": expected, "got": str(exc), "ok": False})
    return results


def units_left(package, code: str, hold_token: str = "") -> int:
    """Sellable units of one stay type, counting this checkout's own hold as its own."""
    st = package.stay_types.filter(code=code).first()
    return st.units_available(hold_token=hold_token) if st else 0


def split_evenly(amount: Decimal, parts: int) -> list:
    """Split cents across `parts` so the pieces sum to `amount` exactly
    (leftover cents go to the first pieces)."""
    cents = int(quantize(amount) * 100)
    base, extra = divmod(cents, parts)
    return [Decimal(base + (1 if i < extra else 0)) / 100 for i in range(parts)]


@dataclass
class ComponentQuote:
    """Core per traveller + each room unit once + experiences per guest +
    transfers per vehicle. The only discount is the group rule (client,
    26 Oct 2026: one group discount on the total price): its percent comes off
    every part of each guest's share, Core included, rounded per room unit and
    per guest. Linked bookings count the whole group's size.

    Each traveller carries a `share` (their part of the total) and `due_now`;
    shares sum to the total exactly, so a group can pay per person."""
    package: object
    mode: str
    payment_plan: str
    core_price: Decimal
    price_list_version: object = None
    travellers: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    transfers: list = field(default_factory=list)
    core_total: Decimal = Decimal("0")
    rooms_total: Decimal = Decimal("0")
    experiences_total: Decimal = Decimal("0")
    transfers_total: Decimal = Decimal("0")
    subtotal: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_name: str = ""
    discount_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    deposit_required: Decimal | None = None
    amount_due_today: Decimal = Decimal("0")

    @property
    def num_guests(self):
        return len(self.travellers)

    def discount_note(self) -> str:
        if not self.discount_total:
            return ""
        return f"{self.discount_name} ({self.discount_percent.normalize():f}% off the total)"[:200]

    def booking_addons(self) -> list:
        """Room, experience and transfer lines aggregated per code, in the
        shape Booking.addons (receipts, refunds) already reads, so Core × guests
        + these lines − discount_amount reconciles to the total."""
        lines = {}

        def add(code, name, unit, unit_price, quantity, line_total, refundable):
            line = lines.setdefault(code, {"code": code, "name": name, "unit": unit, "unit_price": unit_price,
                                           "quantity": 0, "line_total": "0.00", "refundable": refundable})
            line["quantity"] += quantity
            line["line_total"] = str(quantize(Decimal(line["line_total"]) + Decimal(line_total)))

        for r in self.rooms:
            add(f"stay_{r['stay_type']}", f"{r['name']} · {r['nights']} nights", "per_unit", r["charge"], 1,
                r["charge"], True)
        for t in self.travellers:
            for e in t["experiences"]:
                add(e["code"], e["name"], "per_person", e["price"], 1, e["price"], e["refundable"])
        for tr in self.transfers:
            add(tr["code"], tr["name"], "per_vehicle", tr["price"], tr["vehicles"], tr["charge"], tr["refundable"])
        return list(lines.values())

    def donation_options(self) -> list:
        """The optional Academy donation, priced here like everything else:
        a % of the trip total, added to the first payment (empty while closed)."""
        from payments.donations import CHECKOUT_PERCENTS, donations_open
        if not donations_open():
            return []
        out = []
        for pct in CHECKOUT_PERCENTS:
            amount = quantize(self.total * pct / Decimal("100"))
            out.append({"percent": pct, "amount": str(amount), "total": str(quantize(self.total + amount)),
                        "due_today": str(quantize(self.amount_due_today + amount))})
        return out

    def as_dict(self):
        return {
            "package_id": str(self.package.id),
            "pricing_model": "component",
            "mode": self.mode,
            "payment_plan": self.payment_plan,
            "currency": self.package.currency,
            "core_price_per_person": str(self.core_price),
            "price_list_version": self.price_list_version.number if self.price_list_version else None,
            "num_guests": self.num_guests,
            "travellers": self.travellers,
            "rooms": self.rooms,
            "transfers": self.transfers,
            "core_total": str(self.core_total),
            "rooms_total": str(self.rooms_total),
            "experiences_total": str(self.experiences_total),
            "transfers_total": str(self.transfers_total),
            "subtotal": str(self.subtotal),
            "discount_name": self.discount_name if self.discount_total else "",
            "discount_percent": str(self.discount_percent) if self.discount_total else "0",
            "discount_total": str(self.discount_total),
            "total": str(self.total),
            "average_per_person": str(quantize(self.total / self.num_guests)),
            "accommodation_deposit_percent": str(self.package.accommodation_deposit_percent),
            "deposit_required": str(self.deposit_required) if self.deposit_required is not None else None,
            "amount_due_today": str(self.amount_due_today),
            "balance_after_today": str(quantize(self.total - self.amount_due_today)),
            "donation_options": self.donation_options(),
            # Stored in JSONFields (Booking.quote_snapshot, Lead.quote): ISO string, not a date.
            "final_payment_deadline": (
                self.package.final_payment_deadline.isoformat() if self.package.final_payment_deadline else None
            ),
        }


def compute_component_quote(package, *, mode, travellers, rooms, payment_plan, check_availability=True,
                            hold_token="", catalog: Catalog | None = None, group_size: int = 0) -> ComponentQuote:
    """Price a component selection server-side.

    travellers: [{"name": str, "experiences": [addon codes], "adult": bool}] —
        list index is the traveller's id; each one is exactly one Core ticket.
        Experiences include at most one airport transfer (a per-vehicle item).
    rooms: [{"stay_type": code, "occupants": [traveller indexes]}] — each
        entry is one unit, charged once whatever its occupancy.

    Installment plan (Change Request v4 §6): Core, experiences and transfers
    are due in full now; each room needs accommodation_deposit_percent now.

    group_size: guests in the whole group when this booking is linked to
    others (one Airbnb each); the discount tier uses the larger of it and n.
    """
    import math

    from bookings.models import Booking
    from packages.models import TravelPackage

    if not package.is_active:
        raise QuoteError("This tour is not open for booking.")
    if package.pricing_model != TravelPackage.PricingModel.COMPONENT:
        raise QuoteError("This tour does not use component pricing.")
    cat = catalog or live_catalog(package)
    if cat.core_price is None:
        raise QuoteError("This tour has no Core price configured.")
    n = len(travellers or [])
    if n < 1:
        raise QuoteError("At least one traveller is required.")
    if mode not in Booking.Mode.values:
        raise QuoteError("Choose Single, Couple or Group.")
    if mode == Booking.Mode.SINGLE and n != 1:
        raise QuoteError("Single is for one traveller — choose Couple or Group.")
    if mode == Booking.Mode.COUPLE and n != 2:
        raise QuoteError("A couple booking is for exactly two travellers.")
    # Stay only (client, 24 and 26 Oct 2026): an Airbnb without the event, from
    # its own stay-only units and dates, so no Core ticket and no event experiences.
    stay_only = mode == Booking.Mode.STAY
    if stay_only and not rooms:
        raise QuoteError("Choose an Airbnb for your stay.")
    if payment_plan not in (Booking.PaymentPlan.FULL, Booking.PaymentPlan.INSTALLMENT):
        raise QuoteError("Unknown payment plan.")
    installment = payment_plan == Booking.PaymentPlan.INSTALLMENT
    if installment and not installments_open(package):
        raise QuoteError("Paying accommodation in instalments is not available for this booking — please pay in full.")
    if installment and not rooms:
        raise QuoteError("Only accommodation can be paid in instalments; Core tickets and experiences are paid in full.")

    core = Decimal("0.00") if stay_only else quantize(cat.core_price)
    quote = ComponentQuote(package=package, mode=mode, payment_plan=payment_plan, core_price=core,
                           price_list_version=cat.version)
    label = lambda i: (travellers[i].get("name") or "").strip() or f"Guest {i + 1}"
    shares = [{"core": core, "extras": Decimal("0"), "room": Decimal("0"), "room_deposit": Decimal("0"),
               "transfer": Decimal("0")} for _ in range(n)]

    # Group discount: the highest rule the group size qualifies for.
    qualifying = [g for g in cat.group_discounts if g[2] and max(n, group_size) >= g[2]]
    if qualifying:
        quote.discount_percent, quote.discount_name, _ = max(qualifying, key=lambda g: g[0])
    deposit_pct = package.accommodation_deposit_percent if installment else Decimal("100")

    # ── Rooms: each unit once; split among its occupants ─────────────────────
    room_of = {}
    stay_types = cat.stay_types
    requested = {}
    for room in rooms or []:
        st = stay_types.get(room.get("stay_type"))
        if st is None or not st.on_sale:
            raise QuoteError(f"'{room.get('stay_type')}' is not available.")
        if stay_only and not st.stay_only:
            raise QuoteError(f"{st.name} is sold with the event. Choose a stay-only Airbnb.")
        if not stay_only and st.stay_only:
            raise QuoteError(f"{st.name} is a stay without the event. Choose Stay only to book it.")
        occupants = room.get("occupants") or []
        if any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n for i in occupants):
            raise QuoteError("Every room occupant must be one of the travellers and hold a Core ticket.")
        if len(set(occupants)) != len(occupants) or room_of.keys() & set(occupants):
            raise QuoteError("Each traveller can be assigned to one room only.")
        if len(occupants) > st.capacity:
            raise QuoteError(f"{st.name} sleeps at most {st.capacity}.")
        if len(occupants) < max(st.min_occupancy, 1):
            raise QuoteError(f"{st.name} needs {max(st.min_occupancy, 1)} guests.")
        requested[st.code] = requested.get(st.code, 0) + 1
        charge = quantize(st.price_per_night * st.nights)
        discount = quantize(charge * quote.discount_percent / Decimal("100"))
        deposit = quantize((charge - discount) * deposit_pct / Decimal("100"))
        for i, part, dep in zip(occupants, split_evenly(charge - discount, len(occupants)),
                                split_evenly(deposit, len(occupants))):
            room_of[i] = st.name
            shares[i]["room"] += part
            shares[i]["room_deposit"] += dep
        quote.rooms.append({
            "stay_type": st.code, "name": st.name, "occupants": occupants,
            "price_per_night": str(quantize(st.price_per_night)), "nights": st.nights, "check_in": st.check_in,
            "charge": str(charge), "discount": str(discount), "deposit": str(deposit),
        })
        quote.rooms_total += charge
        quote.discount_total += discount

    # One Airbnb per booking, and nobody on it left without a room: a party
    # too big for one unit books separately (client feedback, Oct 2026).
    airbnbs = [r for r in quote.rooms if getattr(stay_types[r["stay_type"]], "kind", "") == "airbnb"]
    if len(airbnbs) > 1:
        raise QuoteError("Only one Airbnb per booking. Larger groups can make separate bookings.")
    if airbnbs and len(room_of) < n:
        raise QuoteError(f"{airbnbs[0]['name']} doesn't fit all {n} guests. Please choose accommodation that fits "
                         "everyone, or make separate bookings.")

    if check_availability:
        for code, count in requested.items():
            left = units_left(package, code, hold_token)
            if count > left:
                raise QuoteError(
                    f"{stay_types[code].name} is sold out." if left == 0
                    else f"Only {left} × {stay_types[code].name} left."
                )

    # ── Experiences per guest; transfers pooled into vehicles ────────────────
    addons = cat.experiences
    riders = {}
    for i, t in enumerate(travellers):
        codes = list(dict.fromkeys(t.get("experiences") or []))
        unknown = [c for c in codes if c not in addons]
        if unknown:
            raise QuoteError(f"Unknown experience(s): {', '.join(unknown)}.")
        chosen = [addons[c] for c in codes]
        for a in chosen:
            if stay_only and a.unit != "per_vehicle":
                raise QuoteError(f"{a.name} is part of the event and needs a Core ticket.")
            if a.adults_only and t.get("adult") is not True:
                raise QuoteError(f"{a.name} is for guests aged 18 or over — please confirm {label(i)} is 18+.")
            if a.requires_stay and i not in room_of:
                raise QuoteError(f"{a.name} is for guests staying in Azura accommodation.")
        transfers = [a for a in chosen if a.unit == "per_vehicle"]
        if len(transfers) > 1:
            raise QuoteError(f"Choose one airport transfer for {label(i)}.")
        for a in transfers:
            riders.setdefault(a.code, []).append(i)
        per_person = [a for a in chosen if a.unit != "per_vehicle"]
        subtotal = quantize(sum((a.price for a in per_person), Decimal("0")))
        shares[i]["extras"] = subtotal
        quote.travellers.append({
            "index": i, "name": (t.get("name") or "").strip(), "core": str(core),
            "room": room_of.get(i), "adult": t.get("adult") is True,
            "experiences": [
                {"code": a.code, "name": a.name, "price": str(quantize(a.price)), "refundable": a.refundable}
                for a in per_person
            ],
            "transfer": transfers[0].code if transfers else None,
            "experiences_subtotal": str(subtotal),
        })
        quote.experiences_total += subtotal

    # ponytail: guests on one transfer share vehicles in booking order; ops
    # regroup by flight time. Per-flight vehicle planning if that matters.
    for code, guests in riders.items():
        a = addons[code]
        vehicles = math.ceil(len(guests) / max(a.vehicle_capacity, 1))
        charge = quantize(a.price * vehicles)
        for i, part in zip(guests, split_evenly(charge, len(guests))):
            shares[i]["transfer"] = part
        quote.transfers.append({"code": a.code, "name": a.name, "guests": guests, "vehicles": vehicles,
                                "vehicle_capacity": a.vehicle_capacity,
                                "price": str(quantize(a.price)), "charge": str(charge), "refundable": a.refundable})
        quote.transfers_total += charge

    pct = quote.discount_percent / Decimal("100")
    for t, s in zip(quote.travellers, shares):
        paid_upfront = s["core"] + s["extras"] + s["transfer"]
        # The room part is already discounted per unit; the rest per guest.
        off = quantize(paid_upfront * pct)
        paid_upfront -= off
        quote.discount_total += off
        t.update(room_share=str(s["room"]), transfer_share=str(s["transfer"]),
                 share=str(quantize(paid_upfront + s["room"])),
                 room_deposit=str(s["room_deposit"]), due_now=str(quantize(paid_upfront + s["room_deposit"])))

    quote.core_total = quantize(core * n)
    quote.rooms_total = quantize(quote.rooms_total)
    quote.experiences_total = quantize(quote.experiences_total)
    quote.transfers_total = quantize(quote.transfers_total)
    quote.discount_total = quantize(quote.discount_total)
    quote.subtotal = quantize(quote.core_total + quote.rooms_total + quote.experiences_total + quote.transfers_total)
    quote.total = quantize(quote.subtotal - quote.discount_total)
    quote.amount_due_today = quantize(sum((Decimal(t["due_now"]) for t in quote.travellers), Decimal("0")))
    if installment:
        quote.deposit_required = quote.amount_due_today
    return quote


def build_component_matrix(package, request=None) -> dict:
    """Catalogue for the trip builder (the live price list). Display only:
    totals always come from the quote endpoint, never client-side arithmetic.
    Stay details and photos come from the stay rows (not price-versioned)."""
    cat = live_catalog(package)
    url = (lambda u: request.build_absolute_uri(u)) if request else (lambda u: u)
    shown = {
        s.code: {
            "bedding": s.bedding, "breakfast": s.breakfast, "location": s.location,
            "amenities": [a.strip() for a in s.amenities.splitlines() if a.strip()],
            "images": [{"url": url(i.image.url), "alt": i.alt_text} for i in s.images.all()],
            "units_total": s.units.filter(blocked=False).count(),
        }
        for s in package.stay_types.prefetch_related("images")
    }
    return {
        "package_id": str(package.id),
        "pricing_model": "component",
        "price_list_version": cat.version.number if cat.version else None,
        "currency": package.currency,
        "core_price_per_person": str(quantize(cat.core_price)) if cat.core_price is not None else None,
        "tour_start": package.available_from,
        "tour_end": package.available_to,
        "stay_types": [
            {
                "code": s.code, "name": s.name, "kind": s.kind, "capacity": s.capacity,
                "min_occupancy": s.min_occupancy, "price_per_night": str(quantize(s.price_per_night)),
                "nights": s.nights, "unit_total": str(quantize(s.price_per_night * s.nights)),
                "on_sale": s.on_sale, "note": s.note, "stay_only": s.stay_only, "check_in": s.check_in,
                "units_available": units_left(package, s.code) if s.on_sale else 0,
                **shown.get(s.code, {"bedding": "", "breakfast": "", "location": "", "amenities": [], "images": [],
                                     "units_total": 0}),
            }
            for s in cat.stay_types.values()
        ],
        "experiences": [
            {"code": a.code, "name": a.name, "description": a.description or None,
             "price": str(quantize(a.price)), "refundable": a.refundable, "unit": a.unit,
             "adults_only": a.adults_only, "requires_stay": a.requires_stay, "vehicle_capacity": a.vehicle_capacity}
            for a in cat.experiences.values()
        ],
        "group_discounts": [
            {"name": name, "percent": str(pct), "min_travellers": m} for pct, name, m in cat.group_discounts
        ],
        "installments": {
            "enabled": installments_open(package),
            "accommodation_deposit_percent": str(package.accommodation_deposit_percent),
            "final_payment_deadline": package.final_payment_deadline,
        },
        "charge": _charge_info(package),
        "notices": {"fees": _fee_notice()},
        "server_now": timezone.now(),
    }
