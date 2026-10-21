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


# ── Component pricing (Change Request v2) ─────────────────────────────────────

def catalog_snapshot(package) -> dict:
    """The editable catalogue tables (the draft) as JSON-safe data."""
    from packages.models import PackageAddon, PackageDiscountRule

    return {
        "core_price": str(package.base_price_per_person) if package.base_price_per_person is not None else None,
        "stay_types": [
            {"code": s.code, "name": s.name, "kind": s.kind, "capacity": s.capacity, "min_occupancy": s.min_occupancy,
             "price_per_night": str(s.price_per_night), "nights": s.nights, "discount_percent": str(s.discount_percent),
             "on_sale": s.on_sale, "note": s.note}
            for s in package.stay_types.order_by("kind", "order")  # Airbnb first, then hotel
        ],
        "experiences": [
            {"code": a.code, "name": a.name, "description": a.description or "", "price": str(a.price),
             "refundable": a.refundable}
            for a in package.addons.filter(is_active=True, unit=PackageAddon.Unit.PER_PERSON).order_by("order", "name")
        ],
        "bundles": [
            {"name": r.name, "percent": str(r.percent), "members": sorted(a.code for a in r.required_addons.all())}
            for r in package.discount_rules.filter(is_active=True, scope=PackageDiscountRule.Scope.TRAVELLER_BUNDLE)
        ],
    }


@dataclass
class Catalog:
    version: object  # PriceListVersion, or None while nothing is published (prices from the draft tables)
    core_price: Decimal | None
    stay_types: dict
    experiences: dict
    bundles: list


def _catalog(snap: dict, version) -> Catalog:
    from types import SimpleNamespace as NS
    dec = lambda d, *keys: {**d, **{k: Decimal(d[k]) for k in keys}}
    return Catalog(
        version=version,
        core_price=Decimal(snap["core_price"]) if snap.get("core_price") is not None else None,
        stay_types={s["code"]: NS(**dec(s, "price_per_night", "discount_percent")) for s in snap["stay_types"]},
        experiences={e["code"]: NS(**dec(e, "price")) for e in snap["experiences"]},
        bundles=[(Decimal(b["percent"]), b["name"], set(b["members"])) for b in snap["bundles"]],
    )


def live_catalog(package) -> Catalog:
    """Prices customers see: the live published version, or the draft tables
    for a package that has never been published."""
    from packages.models import PriceListVersion
    version = package.price_lists.filter(status=PriceListVersion.Status.LIVE).order_by("-number").first()
    return _catalog(version.snapshot if version else catalog_snapshot(package), version)


def draft_catalog(package) -> Catalog:
    return _catalog(catalog_snapshot(package), None)


def units_left(package, code: str, hold_token: str = "") -> int:
    """Sellable units of one stay type, counting this checkout's own hold as its own."""
    st = package.stay_types.filter(code=code).first()
    return st.units_available(hold_token=hold_token) if st else 0


@dataclass
class ComponentQuote:
    """Core per traveller + each room unit once + experiences per guest.
    Discounts are per component (a unit with its occupants' Core tickets; one
    guest's complete extras bundle), each rounded half-up to cents, then summed."""
    package: object
    mode: str
    payment_plan: str
    core_price: Decimal
    price_list_version: object = None
    travellers: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    core_total: Decimal = Decimal("0")
    rooms_total: Decimal = Decimal("0")
    experiences_total: Decimal = Decimal("0")
    subtotal: Decimal = Decimal("0")
    discount_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    deposit_required: Decimal | None = None
    amount_due_today: Decimal = Decimal("0")

    @property
    def num_guests(self):
        return len(self.travellers)

    def discount_note(self) -> str:
        pct = lambda v: f"{Decimal(v).normalize():f}%"  # "3.00" → "3%"
        notes = list(dict.fromkeys(
            f"Stay package saving ({r['name']}, {pct(r['discount_percent'])})" for r in self.rooms if Decimal(r["discount"])
        ))
        notes += list(dict.fromkeys(t["bundle"] for t in self.travellers if t["bundle"]))
        return "; ".join(notes)[:200]

    def booking_addons(self) -> list:
        """Room and experience lines aggregated per code, in the shape
        Booking.addons (receipts, refunds) already reads, so Core × guests +
        these lines − discount_amount reconciles to the total. Gross of
        discounts, which sit in discount_amount like every bundle discount."""
        lines = {}
        for r in self.rooms:
            line = lines.setdefault(f"stay_{r['stay_type']}", {
                "code": f"stay_{r['stay_type']}", "name": f"{r['name']} · {r['nights']} nights",
                "unit": "per_unit", "unit_price": r["charge"], "quantity": 0, "line_total": "0.00",
                "refundable": True,
            })
            line["quantity"] += 1
            line["line_total"] = str(quantize(Decimal(line["line_total"]) + Decimal(r["charge"])))
        for t in self.travellers:
            for e in t["experiences"]:
                line = lines.setdefault(e["code"], {
                    "code": e["code"], "name": e["name"], "unit": "per_person",
                    "unit_price": e["price"], "quantity": 0, "line_total": "0.00",
                    "refundable": e["refundable"],
                })
                line["quantity"] += 1
                line["line_total"] = str(quantize(Decimal(line["line_total"]) + Decimal(e["price"])))
        return list(lines.values())

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
            "core_total": str(self.core_total),
            "rooms_total": str(self.rooms_total),
            "experiences_total": str(self.experiences_total),
            "subtotal": str(self.subtotal),
            "discount_total": str(self.discount_total),
            "total": str(self.total),
            "average_per_person": str(quantize(self.total / self.num_guests)),
            "deposit_required": str(self.deposit_required) if self.deposit_required is not None else None,
            "amount_due_today": str(self.amount_due_today),
            "balance_after_today": str(quantize(self.total - self.amount_due_today)),
            # Stored in JSONFields (Booking.quote_snapshot, Lead.quote): ISO string, not a date.
            "final_payment_deadline": (
                self.package.final_payment_deadline.isoformat() if self.package.final_payment_deadline else None
            ),
        }


def compute_component_quote(package, *, mode, travellers, rooms, payment_plan, check_availability=True,
                            hold_token="", catalog: Catalog | None = None) -> ComponentQuote:
    """Price a component selection server-side.

    travellers: [{"name": str, "experiences": [addon codes]}] — list index is
        the traveller's id; each one is exactly one Core ticket.
    rooms: [{"stay_type": code, "occupants": [traveller indexes]}] — each
        entry is one unit, charged once whatever its occupancy.
    """
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
    if payment_plan not in (Booking.PaymentPlan.FULL, Booking.PaymentPlan.INSTALLMENT):
        raise QuoteError("Unknown payment plan.")
    if payment_plan == Booking.PaymentPlan.INSTALLMENT and not package.allow_installments:
        raise QuoteError("Installment payment is not available for this tour.")

    core = quantize(cat.core_price)
    quote = ComponentQuote(package=package, mode=mode, payment_plan=payment_plan, core_price=core,
                           price_list_version=cat.version)
    room_of = {}

    # ── Rooms: each unit once; discount = unit + its occupants' Core ─────────
    stay_types = cat.stay_types
    requested = {}
    for room in rooms or []:
        st = stay_types.get(room.get("stay_type"))
        if st is None or not st.on_sale:
            raise QuoteError(f"'{room.get('stay_type')}' is not available.")
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
        discount = quantize((core * len(occupants) + charge) * st.discount_percent / Decimal("100"))
        for i in occupants:
            room_of[i] = st.name
        quote.rooms.append({
            "stay_type": st.code, "name": st.name, "occupants": occupants,
            "price_per_night": str(quantize(st.price_per_night)), "nights": st.nights,
            "charge": str(charge), "discount_percent": str(st.discount_percent), "discount": str(discount),
        })
        quote.rooms_total += charge
        quote.discount_total += discount

    if check_availability:
        for code, count in requested.items():
            left = units_left(package, code, hold_token)
            if count > left:
                raise QuoteError(
                    f"{stay_types[code].name} is sold out." if left == 0
                    else f"Only {left} × {stay_types[code].name} left."
                )

    # ── Experiences per guest; best complete bundle discounted once ──────────
    addons = cat.experiences
    bundles = cat.bundles
    for i, t in enumerate(travellers):
        codes = list(dict.fromkeys(t.get("experiences") or []))
        unknown = [c for c in codes if c not in addons]
        if unknown:
            raise QuoteError(f"Unknown experience(s): {', '.join(unknown)}.")
        chosen = [addons[c] for c in codes]
        subtotal = quantize(sum((a.price for a in chosen), Decimal("0")))
        qualifying = [b for b in bundles if b[2] and b[2] <= set(codes)]
        bundle = max(qualifying, key=lambda b: b[0], default=None)
        bundle_discount = Decimal("0.00")
        if bundle:
            base = sum((a.price for a in chosen if a.code in bundle[2]), Decimal("0"))
            bundle_discount = quantize(base * bundle[0] / Decimal("100"))
        quote.travellers.append({
            "index": i, "name": (t.get("name") or "").strip(), "core": str(core),
            "room": room_of.get(i),
            "experiences": [
                {"code": a.code, "name": a.name, "price": str(quantize(a.price)), "refundable": a.refundable}
                for a in chosen
            ],
            "experiences_subtotal": str(subtotal),
            "bundle": f"{bundle[1]} ({bundle[0]}%)" if bundle else "",
            "bundle_discount": str(bundle_discount),
        })
        quote.experiences_total += subtotal
        quote.discount_total += bundle_discount

    quote.core_total = quantize(core * n)
    quote.rooms_total = quantize(quote.rooms_total)
    quote.experiences_total = quantize(quote.experiences_total)
    quote.discount_total = quantize(quote.discount_total)
    quote.subtotal = quantize(quote.core_total + quote.rooms_total + quote.experiences_total)
    quote.total = quantize(quote.subtotal - quote.discount_total)

    if payment_plan == Booking.PaymentPlan.INSTALLMENT:
        if not package.deposit_minimum:
            raise QuoteError("Installment payment is not configured for this tour.")
        if not installments_open(package):
            raise QuoteError("The final payment deadline has passed — this booking is payable in full.")
        # A cart exactly AT the threshold may pay a deposit; below it pays in full.
        if package.deposit_threshold is not None and quote.total < package.deposit_threshold:
            raise QuoteError(f"Bookings under {quantize(package.deposit_threshold)} are payable in full.")
        quote.deposit_required = deposit_for(package, quote.total, n)
        quote.amount_due_today = quote.deposit_required
    else:
        quote.amount_due_today = quote.total
    return quote


def build_component_matrix(package) -> dict:
    """Catalogue for the trip builder (the live price list). Display only:
    totals always come from the quote endpoint, never client-side arithmetic."""
    cat = live_catalog(package)
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
                "discount_percent": str(s.discount_percent), "on_sale": s.on_sale, "note": s.note,
                "units_available": units_left(package, s.code) if s.on_sale else 0,
            }
            for s in cat.stay_types.values()
        ],
        "experiences": [
            {"code": a.code, "name": a.name, "description": a.description or None,
             "price": str(quantize(a.price)), "refundable": a.refundable}
            for a in cat.experiences.values()
        ],
        "bundles": [
            {"name": name, "percent": str(pct), "members": sorted(members)} for pct, name, members in cat.bundles
        ],
        "installments": {
            "enabled": installments_open(package),
            "deposit_minimum": str(quantize(package.deposit_minimum)) if package.deposit_minimum else None,
            "deposit_unit": package.deposit_unit,
            "deposit_threshold": str(quantize(package.deposit_threshold)) if package.deposit_threshold is not None else None,
            "final_payment_deadline": package.final_payment_deadline,
        },
        "charge": _charge_info(package),
        "notices": {"fees": _fee_notice()},
        "server_now": timezone.now(),
    }
