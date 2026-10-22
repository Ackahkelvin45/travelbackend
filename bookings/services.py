"""
bookings/services.py
--------------------
Booking creation for the option-based (flagship tour) flow.

Everything commercial is computed by bookings.pricing and snapshotted here,
inside one transaction that also records the policy acceptances — a booking
without its acceptances never exists.
"""

import logging
from decimal import Decimal

from django.db import transaction

from .models import Booking, BookingTraveller, Lead, PolicyAcceptance, PolicyDocument
from .pricing import (
    QuoteError, assert_capacity, compute_component_quote, compute_configurable_quote, compute_quote,
)

logger = logging.getLogger(__name__)


class PolicyAcceptanceRequired(Exception):
    def __init__(self, missing_types):
        self.missing_types = missing_types
        super().__init__(f"Missing policy acceptances: {', '.join(missing_types)}")


def required_policy_documents():
    """Every currently-published MANDATORY policy version must be accepted at
    checkout. Optional consents (is_required=False, e.g. media) are recorded
    when given but never block a booking."""
    return list(PolicyDocument.objects.filter(
        is_current=True, published_at__isnull=False, is_required=True,
    ))


def create_option_booking(
    *,
    option,
    visa: bool,
    payment_plan: str,
    contact: dict,
    accepted_policy_types: list,
    user=None,
    ip_address: str | None = None,
) -> Booking:
    """
    Create a fully-snapshotted booking for a hotel/occupancy option.

    Raises QuoteError for pricing problems and PolicyAcceptanceRequired when
    any current policy document was not accepted.
    """
    quote = compute_quote(option, visa=visa, payment_plan=payment_plan)
    package = quote.package
    assert_capacity(package, quote.num_guests)

    documents = required_policy_documents()
    missing = [d.type for d in documents if d.type not in set(accepted_policy_types)]
    if missing:
        raise PolicyAcceptanceRequired(missing)

    with transaction.atomic():
        booking = Booking.objects.create(
            user=user,
            first_name=contact["first_name"],
            last_name=contact["last_name"],
            email=contact["email"],
            phone=contact.get("phone"),
            country=contact.get("country"),
            special_requests=contact.get("special_requests"),
            package=package,
            option=quote.option,
            payment_plan=quote.payment_plan,
            num_guests=quote.num_guests,
            # Departure is a package fact, snapshotted per booking — refund
            # tiers are computed against this date, never against live config.
            travel_date=package.available_from,
            unit_price=quote.effective_price_per_person,
            total_amount=quote.total,
            currency=package.currency,
            option_snapshot={
                "hotel_name": quote.option.hotel_name,
                "star_rating": quote.option.star_rating,
                "occupancy": quote.option.occupancy,
                "occupancy_display": quote.option.get_occupancy_display(),
                "standard_price_per_person": str(quote.standard_price_per_person),
                "effective_price_per_person": str(quote.effective_price_per_person),
            },
            early_bird_applied=quote.early_bird_applied,
            early_bird_discount=quote.early_bird_discount,
            addons=quote.addons,
            refund_tiers_snapshot=package.refund_tiers,
            deposit_required=quote.deposit_required,
        )

        PolicyAcceptance.objects.bulk_create([
            PolicyAcceptance(
                booking=booking,
                document=document,
                email=booking.email,
                ip_address=ip_address,
            )
            for document in documents
        ])

    logger.info(
        "Option booking created: ref=%s option=%s plan=%s total=%s %s eb=%s",
        booking.reference, quote.option.id, payment_plan,
        booking.total_amount, booking.currency, booking.early_bird_applied,
    )
    return booking


def create_configurable_booking(
    *,
    package,
    num_guests: int,
    selected_addon_codes: list,
    payment_plan: str,
    contact: dict,
    accepted_policy_types: list,
    user=None,
    ip_address: str | None = None,
) -> Booking:
    """Create a fully-snapshotted booking for a core_plus_addons package
    (mandatory base + chosen add-ons + the highest qualifying bundle discount).
    Mirrors create_option_booking: prices are server-computed and snapshotted,
    policy acceptances recorded in the same transaction."""
    quote = compute_configurable_quote(
        package, num_guests=num_guests,
        selected_addon_codes=selected_addon_codes, payment_plan=payment_plan,
    )
    assert_capacity(package, num_guests)

    documents = required_policy_documents()
    missing = [d.type for d in documents if d.type not in set(accepted_policy_types)]
    if missing:
        raise PolicyAcceptanceRequired(missing)

    with transaction.atomic():
        booking = Booking.objects.create(
            user=user,
            first_name=contact["first_name"],
            last_name=contact["last_name"],
            email=contact["email"],
            phone=contact.get("phone"),
            country=contact.get("country"),
            special_requests=contact.get("special_requests"),
            package=package,
            option=None,
            payment_plan=quote.payment_plan,
            num_guests=quote.num_guests,
            travel_date=package.available_from,
            unit_price=quote.base_price_per_person,
            total_amount=quote.total,
            currency=package.currency,
            option_snapshot={"core_tour": True, "base_price_per_person": str(quote.base_price_per_person)},
            addons=quote.addons,
            discount_amount=quote.discount_amount,
            discount_note=(f"{quote.discount_note} ({quote.discount_percent}%)" if quote.discount_note else ""),
            refund_tiers_snapshot=package.refund_tiers,
            deposit_required=quote.deposit_required,
        )
        PolicyAcceptance.objects.bulk_create([
            PolicyAcceptance(booking=booking, document=document, email=booking.email, ip_address=ip_address)
            for document in documents
        ])

    logger.info(
        "Configurable booking created: ref=%s pkg=%s guests=%s plan=%s subtotal=%s discount=%s total=%s %s",
        booking.reference, package.id, num_guests, payment_plan,
        quote.subtotal, quote.discount_amount, booking.total_amount, booking.currency,
    )
    return booking


class IllegalTransition(Exception):
    pass


# ── Component bookings: room holds, checkout, traveller moves ────────────────

def create_hold(package, stay_codes: list, *, replace_token: str = ""):
    """Hold one unit per entry in ``stay_codes`` for OpsConfig.hold_minutes.
    Returns (token, held_until, units). Rows are claimed under a row lock so
    two checkouts can never hold the same unit; a previous hold of the same
    checkout (``replace_token``) is released first."""
    import secrets
    from collections import Counter
    from datetime import timedelta

    from django.utils import timezone

    from packages.models import RoomUnit
    from payments.models import OpsConfig

    if not stay_codes:
        raise QuoteError("Choose at least one room to hold.")
    now = timezone.now()
    held_until = now + timedelta(minutes=OpsConfig.get().hold_minutes)
    token = secrets.token_urlsafe(16)
    claimed = []
    with transaction.atomic():
        if replace_token:
            release_hold(replace_token)
        for code, count in Counter(stay_codes).items():
            units = list(
                RoomUnit.objects.available(now)
                .select_for_update(skip_locked=True, of=("self",))
                .filter(stay_type__package=package, stay_type__code=code, stay_type__on_sale=True)
                .select_related("stay_type")[:count]
            )
            if len(units) < count:
                raise QuoteError(f"Sorry — not enough '{code}' rooms are left.")
            claimed += units
        RoomUnit.objects.filter(pk__in=[u.pk for u in claimed]).update(
            booking=None, hold_token=token, held_until=held_until,
        )
    return token, held_until, claimed


def renew_room_hold(booking) -> None:
    """Before each payment attempt on an unconfirmed booking, hold its rooms
    for another checkout window. Rooms resold after a lapsed window cannot be
    renewed: the guest chooses again (QuoteError)."""
    from datetime import timedelta

    from django.utils import timezone

    from packages.models import RoomUnit
    from payments.models import OpsConfig

    expected = len((booking.quote_snapshot or {}).get("rooms", []))
    if not expected or booking.status != Booking.Status.PENDING:
        return
    with transaction.atomic():
        units = list(RoomUnit.objects.select_for_update().filter(booking=booking, blocked=False))
        if len(units) < expected:
            raise QuoteError("Your room is no longer available — please start a new booking to choose again.")
        RoomUnit.objects.filter(pk__in=[u.pk for u in units]).update(
            held_until=timezone.now() + timedelta(minutes=OpsConfig.get().hold_minutes),
        )


def release_hold(token: str) -> int:
    """Give back a checkout hold that has not become a booking."""
    from packages.models import RoomUnit
    if not token:
        return 0
    return RoomUnit.objects.filter(hold_token=token, booking__isnull=True).update(hold_token="", held_until=None)


def create_component_booking(
    *, package, mode: str, travellers: list, rooms: list, payment_plan: str, hold_token: str,
    contact: dict, accepted_policy_types: list, marketing_consent: bool = False,
    pay_separately: bool = False, user=None, ip_address: str | None = None,
) -> Booking:
    """Turn a room hold into a priced, snapshotted booking with one
    BookingTraveller (= one Core ticket) per guest. The units stay held only
    for the checkout window (no unpaid room holds, Change Request v4 §6) and
    become sold once the required payment is verified."""
    from datetime import timedelta

    from django.utils import timezone

    from packages.models import RoomUnit
    from payments.models import OpsConfig

    quote = compute_component_quote(
        package, mode=mode, travellers=travellers, rooms=rooms,
        payment_plan=payment_plan, check_availability=False,  # the hold is the availability
    )
    assert_capacity(package, quote.num_guests)
    if mode == Booking.Mode.COUPLE and not all((t.get("name") or "").strip() for t in travellers):
        raise QuoteError("Please give both guests' names.")
    if pay_separately and mode != Booking.Mode.GROUP:
        raise QuoteError("Paying separately is for group bookings.")

    # What the checkout payment covers: everything due now, or — when members
    # pay separately — the organiser's own part plus every room deposit, so
    # rooms are secured by the first payment (Change Request v4 §4, §6).
    portions = [
        Decimal(t["due_now"]) if (i == 0 or not pay_separately) else Decimal(t["room_deposit"])
        for i, t in enumerate(quote.travellers)
    ]
    deposit_required = sum(portions, Decimal("0")) if (pay_separately or quote.deposit_required) else None

    documents = required_policy_documents()
    accepted = set(accepted_policy_types)
    missing = [d.type for d in documents if d.type not in accepted]
    if missing:
        raise PolicyAcceptanceRequired(missing)
    optional = list(PolicyDocument.objects.filter(
        is_current=True, published_at__isnull=False, is_required=False, type__in=accepted,
    ))

    now = timezone.now()
    with transaction.atomic():
        held = list(
            RoomUnit.objects.select_for_update(of=("self",))
            .filter(hold_token=hold_token, booking__isnull=True, held_until__gt=now,
                    stay_type__package=package)
            .select_related("stay_type")
        ) if hold_token else []
        by_code = {}
        for unit in held:
            by_code.setdefault(unit.stay_type.code, []).append(unit)
        if sorted(r["stay_type"] for r in quote.rooms) != sorted(u.stay_type.code for u in held):
            raise QuoteError("Your room hold has expired or changed — please choose your rooms again.")

        lead_name = f"{contact['first_name']} {contact['last_name']}".strip()
        booking = Booking.objects.create(
            user=user,
            first_name=contact["first_name"], last_name=contact["last_name"], email=contact["email"],
            phone=contact.get("phone"), country=contact.get("country"),
            special_requests=contact.get("special_requests"),
            package=package, option=None, mode=mode, marketing_consent=marketing_consent,
            photo_consent=any(d.type == PolicyDocument.Type.MEDIA for d in optional),
            payment_plan=quote.payment_plan, num_guests=quote.num_guests,
            travel_date=package.available_from,
            unit_price=quote.core_price, total_amount=quote.total, currency=package.currency,
            option_snapshot={"component": True, "core_price_per_person": str(quote.core_price)},
            addons=quote.booking_addons(),
            discount_amount=quote.discount_total, discount_note=quote.discount_note(),
            quote_snapshot=quote.as_dict(),
            price_list_version=quote.price_list_version,
            refund_tiers_snapshot=package.refund_tiers,
            deposit_required=deposit_required, pay_separately=pay_separately,
        )
        held_until = now + timedelta(minutes=OpsConfig.get().hold_minutes)
        unit_of = {}
        for room in quote.rooms:
            unit = by_code[room["stay_type"]].pop()
            unit.booking, unit.hold_token, unit.held_until = booking, "", held_until
            unit.save(update_fields=["booking", "hold_token", "held_until"])
            room["unit"] = unit.label
            for i in room["occupants"]:
                unit_of[i] = unit
        booking.quote_snapshot = quote.as_dict()  # now with unit labels
        booking.save(update_fields=["quote_snapshot"])

        BookingTraveller.objects.bulk_create([
            BookingTraveller(
                booking=booking, is_lead=(i == 0), unit=unit_of.get(i), position=i,
                share=Decimal(quote.travellers[i]["share"]), checkout_portion=portions[i],
                full_name=(t.get("name") or "").strip() or (lead_name if i == 0 else f"Guest {i + 1}"),
                experiences=[e["code"] for e in quote.travellers[i]["experiences"]]
                + ([quote.travellers[i]["transfer"]] if quote.travellers[i]["transfer"] else []),
            )
            for i, t in enumerate(travellers)
        ])
        PolicyAcceptance.objects.bulk_create([
            PolicyAcceptance(booking=booking, document=d, email=booking.email, ip_address=ip_address)
            for d in documents + optional
        ])
        Lead.objects.filter(email__iexact=booking.email, booking__isnull=True).exclude(
            stage=Lead.Stage.LOST,
        ).update(stage=Lead.Stage.WON, booking=booking)

    logger.info(
        "Component booking created: ref=%s pkg=%s mode=%s guests=%s rooms=%s total=%s %s",
        booking.reference, package.id, mode, quote.num_guests, len(quote.rooms),
        booking.total_amount, booking.currency,
    )
    return booking


def send_balance_reminder(booking) -> bool:
    """Email the balance reminder now (staff action). Paid, cancelled and
    deadline-less bookings are skipped; returns whether an email went out."""
    from django.utils import timezone

    from bookings.management.commands.send_payment_reminders import _build_reminder_html
    from payments.email import deliver

    deadline = booking.effective_payment_deadline
    if booking.status != Booking.Status.CONFIRMED or booking.is_paid or not deadline:
        return False
    days_left = max((deadline - timezone.localdate()).days, 0)
    return deliver("reminder_manual", booking.email,
                   f"Balance due {deadline.strftime('%b %d')} – {booking.reference} | Azura Travels",
                   _build_reminder_html(booking, days_left), booking=booking)


def publish_price_list(package, *, user=None, note: str = ""):
    """Snapshot the draft catalogue as the next live price list; the previous
    live version is retired. Existing bookings keep the version they bought."""
    from packages.models import PriceListVersion
    from .pricing import catalog_snapshot

    with transaction.atomic():
        current = list(PriceListVersion.objects.select_for_update().filter(package=package).order_by("-number")[:1])
        package.price_lists.filter(status=PriceListVersion.Status.LIVE).update(status=PriceListVersion.Status.RETIRED)
        return PriceListVersion.objects.create(
            package=package, number=(current[0].number + 1) if current else 1,
            snapshot=catalog_snapshot(package), note=note, published_by=user,
        )


def reassign_unit(unit, target):
    """Move a whole allocation (booking, hold expiry and its guests) to a free
    unit of the same stay type — e.g. when a unit has to be blocked."""
    from packages.models import RoomUnit

    with transaction.atomic():
        unit, target = (RoomUnit.objects.select_for_update().get(pk=u.pk) for u in (unit, target))
        if not unit.booking_id:
            raise IllegalTransition(f"{unit.label} has no booking to move.")
        if target.stay_type_id != unit.stay_type_id:
            raise IllegalTransition("Move to a unit of the same stay type (prices are per stay type).")
        if not RoomUnit.objects.available().filter(pk=target.pk).exists():
            raise IllegalTransition(f"{target.label} is not free.")
        target.booking_id, target.held_until, target.hold_token = unit.booking_id, unit.held_until, ""
        target.save(update_fields=["booking", "held_until", "hold_token"])
        BookingTraveller.objects.filter(unit=unit).update(unit=target)
        unit.booking, unit.held_until, unit.hold_token = None, None, ""
        unit.save(update_fields=["booking", "held_until", "hold_token"])
    return target


def move_traveller(traveller, unit) -> BookingTraveller:
    """Staff: move a guest to another of the booking's rooms (or none). The
    guest keeps their single Core ticket — nothing is re-priced or re-issued."""
    with transaction.atomic():
        Booking.objects.select_for_update().get(pk=traveller.booking_id)
        if unit is not None:
            if unit.booking_id != traveller.booking_id:
                raise IllegalTransition("Travellers can only move between this booking's own rooms.")
            if unit.occupants.exclude(pk=traveller.pk).count() >= unit.stay_type.capacity:
                raise IllegalTransition(f"{unit.label} is full.")
        traveller.unit = unit
        traveller.save(update_fields=["unit"])
    return traveller


def cancel_booking(booking, *, reason: str, refund_reason: str = "", at=None) -> Booking:
    """
    The one legal way to cancel a booking. Sets the cancellation facts and,
    when money was paid, materializes the computed refund as pending legs for
    the operator to execute.

    ``reason`` is a Booking.CancellationReason value — customer-requested,
    expired, overdue forfeit, or admin. ``at`` fixes the instant the refund
    tier is judged from (the policy uses the time a request was RECEIVED).
    """
    from django.utils import timezone

    from payments.refunds import create_pending_refunds

    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking.pk)

        if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
            raise IllegalTransition(
                f"A {booking.status} booking cannot be cancelled."
            )

        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = reason
        booking.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])
        # Rooms go back on sale; the quote snapshot keeps what was booked.
        booking.travellers.update(unit=None)
        booking.room_units.update(booking=None, hold_token="", held_until=None)

        legs = []
        if booking.amount_paid > booking.amount_refunded:
            legs = create_pending_refunds(
                booking,
                reason=refund_reason or f"Cancellation ({reason})",
                at=at,
                full=(reason == Booking.CancellationReason.ORGANIZER),
            )

    logger.info(
        "Booking %s cancelled (%s), %s pending refund leg(s) created.",
        booking.reference, reason, len(legs),
    )
    return booking


# ── Customer cancellation requests (Refund Policy, Part B) ───────────────────

def request_cancellation(booking, *, reason: str = "") -> "CancellationRequest":
    """Record the guest's request the moment it arrives, with the refund they
    were quoted at that instant, and acknowledge it by email. Payments are
    frozen until staff resolve it (see payments initialize)."""
    from django.utils import timezone

    from payments.refunds import compute_refund
    from .models import CancellationRequest

    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
        raise IllegalTransition(f"A {booking.status} booking cannot be cancelled.")
    if booking.pending_cancellation:
        raise IllegalTransition("A cancellation request is already under review.")

    now = timezone.now()
    quote = compute_refund(booking, at=now)
    req = CancellationRequest.objects.create(
        booking=booking,
        reason=reason or "",
        refund_quote={**quote["breakdown"], "refund_total": str(quote["refund_total"])},
    )
    from payments.email import send_cancellation_acknowledgment
    send_cancellation_acknowledgment(req)
    logger.info("Cancellation requested for %s (quote %s)", booking.reference, quote["refund_total"])
    return req


def approve_cancellation(req, *, by_user, note: str = "") -> "CancellationRequest":
    """Cancel the booking as of the request time and create the refund legs."""
    from django.utils import timezone

    from .models import CancellationRequest

    if req.status != CancellationRequest.Status.PENDING:
        raise IllegalTransition("This request has already been resolved.")
    cancel_booking(
        req.booking,
        reason=Booking.CancellationReason.CUSTOMER,
        refund_reason="Customer cancellation request",
        at=req.requested_at,
    )
    req.status = CancellationRequest.Status.APPROVED
    req.resolved_at, req.resolved_by, req.staff_note = timezone.now(), by_user, note
    req.save(update_fields=["status", "resolved_at", "resolved_by", "staff_note"])
    from payments.email import send_cancellation_outcome
    send_cancellation_outcome(req)
    return req


def reject_cancellation(req, *, by_user, note: str = "") -> "CancellationRequest":
    """Keep the booking; payments reopen."""
    from django.utils import timezone

    from .models import CancellationRequest

    if req.status != CancellationRequest.Status.PENDING:
        raise IllegalTransition("This request has already been resolved.")
    req.status = CancellationRequest.Status.REJECTED
    req.resolved_at, req.resolved_by, req.staff_note = timezone.now(), by_user, note
    req.save(update_fields=["status", "resolved_at", "resolved_by", "staff_note"])
    from payments.email import send_cancellation_outcome
    send_cancellation_outcome(req)
    return req


# ── Add-on cancellation (experience OR hotel) — 50% fee (owner decision, 28 Sep 2026) ─

ADDON_CANCELLATION_FEE_PERCENT = Decimal("50")


def remove_addon(booking, *, code: str, by_email: str, note: str = "") -> dict:
    """Cancel one add-on on a confirmed/pending booking.

    The add-on line is replaced by a non-refundable "cancellation fee" line
    worth ADDON_CANCELLATION_FEE_PERCENT of it, so the bundle discount is left
    untouched (never clawed back) and receipts still reconcile. If the guest
    has already paid more than the new total, the difference becomes pending
    refund legs; otherwise their balance simply drops.
    """
    from django.utils import timezone

    from payments.money import quantize
    from payments.refunds import create_fixed_refund

    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
        raise IllegalTransition(f"A {booking.status} booking cannot be changed.")
    if booking.pending_cancellation:
        raise IllegalTransition("A cancellation request is under review — resolve it first.")

    lines = list(booking.addons or [])
    line = next((l for l in lines if l.get("code") == code), None)
    if line is None:
        raise IllegalTransition("That add-on is not on this booking.")
    charge = quantize(Decimal(str(line.get("line_total", "0"))))
    if charge <= 0 or code.endswith("_cancellation_fee") or line.get("unit") in ("per_unit", "per_vehicle"):
        raise IllegalTransition("That item cannot be cancelled.")

    fee = quantize(charge * ADDON_CANCELLATION_FEE_PERCENT / Decimal("100"))
    fee_line = {
        "code": f"{code}_cancellation_fee",
        "name": f"Cancellation fee — {line.get('name', code)} ({ADDON_CANCELLATION_FEE_PERCENT:.0f}%)",
        "unit": "per_booking", "unit_price": str(fee), "quantity": 1, "line_total": str(fee),
        "refundable": False,
    }

    # Cancelling the hotel (a pick-one group) means "No Accra Hotel": put the
    # group's $0 default back so receipts say so explicitly.
    fallback = None
    addon = booking.package.addons.filter(code=code).select_related("group").first()
    if addon and addon.group and addon.group.selection == addon.group.Selection.SINGLE:
        default = addon.group.addons.filter(is_default=True, is_active=True).exclude(code=code).first()
        if default and not any(l.get("code") == default.code for l in lines):
            fallback = _addon_line(default, booking.num_guests)

    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        old_total = quantize(booking.total_amount)
        booking.addons = [l for l in lines if l.get("code") != code] + ([fallback] if fallback else []) + [fee_line]
        booking.total_amount = quantize(old_total - charge + fee)
        overpaid = quantize(booking.amount_paid - booking.amount_refunded - booking.total_amount)
        booking.addon_changes = [*(booking.addon_changes or []), {
            "code": code, "name": line.get("name"), "charge": str(charge), "fee": str(fee),
            "refund": str(max(overpaid, Decimal("0"))), "old_total": str(old_total),
            "new_total": str(booking.total_amount), "note": note,
            "at": timezone.now().isoformat(), "by": by_email,
        }]
        booking.save(update_fields=["addons", "total_amount", "addon_changes", "updated_at"])
        legs = create_fixed_refund(
            booking, overpaid,
            reason=f"{line.get('name', code)} cancelled — {ADDON_CANCELLATION_FEE_PERCENT:.0f}% fee retained",
            breakdown={"basis": "addon_cancellation", "addon": code, "charge": str(charge), "fee": str(fee)},
        ) if overpaid > 0 else []

    from payments.email import send_addon_removed
    send_addon_removed(booking, line, fee, sum((l.amount for l in legs), Decimal("0")))
    logger.info("Add-on %s removed from %s: charge %s fee %s refund %s", code, booking.reference, charge, fee, overpaid)
    return {"charge": charge, "fee": fee, "refund": max(overpaid, Decimal("0")), "legs": legs}


# ── Adding an experience after booking (owner decision, 28 Sep 2026) ────────

ADDON_ADD_CUTOFF_DAYS = 7


def _addon_line(addon, num_guests: int) -> dict:
    from payments.money import quantize
    qty = num_guests if addon.unit == addon.Unit.PER_PERSON else 1
    total = quantize(addon.price * qty)
    return {
        "code": addon.code, "name": addon.name, "description": addon.description, "unit": addon.unit,
        "unit_price": str(quantize(addon.price)), "quantity": qty, "line_total": str(total),
        "refundable": addon.refundable,
    }


def addon_additions_open(booking):
    """(open: bool, reason: str|None, open_until: date|None)."""
    from datetime import date, timedelta
    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
        return False, f"A {booking.status} booking cannot be changed.", None
    if booking.pending_cancellation:
        return False, "A cancellation request is under review.", None
    open_until = booking.travel_date - timedelta(days=ADDON_ADD_CUTOFF_DAYS)
    if date.today() > open_until:
        return False, f"Experiences can be added up to {ADDON_ADD_CUTOFF_DAYS} days before departure.", open_until
    return True, None, open_until


def available_addons(booking) -> list:
    """Priced experiences the guest could still add: active, in a pick-any
    group or ungrouped (hotel choice can't change this way), not already on
    the booking. Priced at face value — paid items are never repriced."""
    on_booking = {l.get("code") for l in (booking.addons or [])}
    out = []
    for a in booking.package.addons.filter(is_active=True).select_related("group").order_by("order", "name"):
        if a.group and a.group.selection == a.group.Selection.SINGLE:
            continue
        if a.price <= 0 or a.code in on_booking:
            continue
        out.append(_addon_line(a, booking.num_guests))
    return out


def available_component_addons(booking) -> list:
    """Experiences and transfers a component booking can still add, at live
    prices, each with the guests eligible for it (Change Request v4 §6, §8:
    paid when purchased through the guest dashboard)."""
    from payments.money import quantize
    from .pricing import live_catalog

    catalog = live_catalog(booking.package).experiences
    transfer_codes = {c for c, a in catalog.items() if a.unit == "per_vehicle"}
    travellers = list(booking.travellers.all())
    out = []
    for a in catalog.values():
        eligible = [
            str(t.id) for t in travellers
            if a.code not in t.experiences and (not a.requires_stay or t.unit_id)
            and not (a.code in transfer_codes and transfer_codes & set(t.experiences))  # one transfer per guest
        ]
        if eligible:
            out.append({"code": a.code, "name": a.name, "description": a.description or None, "unit": a.unit,
                        "unit_price": str(quantize(a.price)), "adults_only": a.adults_only,
                        "vehicle_capacity": a.vehicle_capacity, "refundable": a.refundable,
                        "eligible_travellers": eligible})
    return out


def quote_component_addon(booking, code: str, traveller_ids: list, adult: bool = False) -> dict:
    """Price one add-on for chosen guests: per guest, or per vehicle shared by
    the guests in this purchase. Raises IllegalTransition."""
    import math
    from payments.money import quantize

    ok, reason, _ = addon_additions_open(booking)
    if not ok:
        raise IllegalTransition(reason)
    option = next((o for o in available_component_addons(booking) if o["code"] == code), None)
    ids = list(dict.fromkeys(str(i) for i in traveller_ids or []))
    if option is None or not ids or not set(ids) <= set(option["eligible_travellers"]):
        raise IllegalTransition("That can't be added for those guests.")
    if option["adults_only"] and not adult:
        raise IllegalTransition(f"{option['name']} is for guests aged 18 or over — please confirm.")
    price = Decimal(option["unit_price"])
    qty = math.ceil(len(ids) / max(option["vehicle_capacity"], 1)) if option["unit"] == "per_vehicle" else len(ids)
    return {"code": code, "name": option["name"], "unit": option["unit"], "unit_price": option["unit_price"],
            "quantity": qty, "line_total": str(quantize(price * qty)), "refundable": option["refundable"],
            "travellers": ids}


def quote_addon_addition(booking, code: str) -> dict:
    ok, reason, _ = addon_additions_open(booking)
    if not ok:
        raise IllegalTransition(reason)
    line = next((l for l in available_addons(booking) if l["code"] == code), None)
    if line is None:
        raise IllegalTransition("That experience can't be added to this booking.")
    return line


def _apply_component_addon(booking, payment) -> bool:
    """Component booking: the paid add-on joins each chosen guest; their share
    and covered portion grow by their part, so per-person balances stay right."""
    from payments.money import quantize
    from .pricing import split_evenly

    travellers = list(booking.travellers.filter(pk__in=payment.addon_travellers).order_by("position"))
    travellers = [t for t in travellers if payment.addon_code not in t.experiences]
    if not travellers:
        return False
    addon = booking.package.addons.filter(code=payment.addon_code).first()
    per_vehicle = addon is not None and addon.unit == addon.Unit.PER_VEHICLE
    lines = list(booking.addons or [])
    line = next((l for l in lines if l.get("code") == payment.addon_code), None)
    if line is None:
        line = {"code": payment.addon_code, "name": addon.name if addon else payment.addon_code,
                "unit": addon.unit if addon else "per_person", "unit_price": str(addon.price if addon else payment.amount),
                "quantity": 0, "line_total": "0.00", "refundable": addon.refundable if addon else True}
        lines.append(line)
    line["quantity"] += -(-len(travellers) // max(addon.vehicle_capacity, 1)) if per_vehicle else len(travellers)
    line["line_total"] = str(quantize(Decimal(line["line_total"]) + payment.amount))
    for t, part in zip(travellers, split_evenly(payment.amount, len(travellers))):
        t.experiences = [*t.experiences, payment.addon_code]
        t.share += part
        t.checkout_portion += part
        t.save(update_fields=["experiences", "share", "checkout_portion"])
    booking.addons = lines
    booking.total_amount = quantize(booking.total_amount + payment.amount)
    return True


def apply_paid_addon(booking, payment) -> bool:
    """Called under the booking lock once an ADDON payment succeeded: put the
    line on the booking and raise the total by exactly what was paid, so the
    balance is unchanged and the payment is never seen as an over-payment."""
    from payments.money import quantize
    code = payment.addon_code
    if code and payment.addon_travellers:
        return _apply_component_addon(booking, payment)
    if not code or any(l.get("code") == code for l in (booking.addons or [])):
        return False
    addon = booking.package.addons.filter(code=code).first()
    line = _addon_line(addon, booking.num_guests) if addon else {"code": code, "name": code, "unit": "per_booking",
                                                                  "unit_price": str(payment.amount), "quantity": 1, "refundable": True}
    line["line_total"] = str(quantize(payment.amount))          # what was actually paid
    booking.addons = [*(booking.addons or []), line]
    booking.total_amount = quantize(booking.total_amount + payment.amount)
    return True


def ticket_outstanding(traveller) -> Decimal:
    """What this guest still owes (their share, per Booking.traveller_balances)."""
    return next(r[3] for r in traveller.booking.traveller_balances() if r[0].pk == traveller.pk)


def check_in_ticket(traveller, *, by) -> None:
    """Event access (Change Request v4 §6): a guest checks in only when their
    share is paid, or staff recorded an exception. Raises IllegalTransition."""
    from django.utils import timezone

    if traveller.forfeited_at:
        raise IllegalTransition(f"{traveller.full_name}'s ticket was forfeited.")
    if traveller.booking.status not in (Booking.Status.CONFIRMED, Booking.Status.COMPLETED):
        raise IllegalTransition(f"Booking {traveller.booking.reference} is {traveller.booking.status}.")
    owed = ticket_outstanding(traveller)
    if owed > 0 and not traveller.access_exception:
        raise IllegalTransition(
            f"{traveller.full_name} owes {traveller.booking.currency} {owed:,.2f}: take payment or record an exception."
        )
    if not traveller.checked_in_at:
        traveller.checked_in_at, traveller.checked_in_by = timezone.now(), by
        traveller.save(update_fields=["checked_in_at", "checked_in_by"])


def forfeit_ticket(traveller) -> None:
    """Record a ticket as forfeited for non-payment at event start. Moves no
    money and cancels nothing else; refunds follow the policy via staff."""
    from django.utils import timezone

    if traveller.checked_in_at:
        raise IllegalTransition(f"{traveller.full_name} is already checked in.")
    if traveller.access_exception or ticket_outstanding(traveller) <= 0:
        raise IllegalTransition(f"{traveller.full_name} is paid or has an exception.")
    if not traveller.forfeited_at:
        traveller.forfeited_at = timezone.now()
        traveller.save(update_fields=["forfeited_at"])
