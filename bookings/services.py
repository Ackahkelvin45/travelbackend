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

from .models import Booking, PolicyAcceptance, PolicyDocument
from .pricing import QuoteError, compute_configurable_quote, compute_quote, assert_capacity

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
    if charge <= 0 or code.endswith("_cancellation_fee"):
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


def quote_addon_addition(booking, code: str) -> dict:
    ok, reason, _ = addon_additions_open(booking)
    if not ok:
        raise IllegalTransition(reason)
    line = next((l for l in available_addons(booking) if l["code"] == code), None)
    if line is None:
        raise IllegalTransition("That experience can't be added to this booking.")
    return line


def apply_paid_addon(booking, payment) -> bool:
    """Called under the booking lock once an ADDON payment succeeded: put the
    line on the booking and raise the total by exactly what was paid, so the
    balance is unchanged and the payment is never seen as an over-payment."""
    from payments.money import quantize
    code = payment.addon_code
    if not code or any(l.get("code") == code for l in (booking.addons or [])):
        return False
    addon = booking.package.addons.filter(code=code).first()
    line = _addon_line(addon, booking.num_guests) if addon else {"code": code, "name": code, "unit": "per_booking",
                                                                  "unit_price": str(payment.amount), "quantity": 1, "refundable": True}
    line["line_total"] = str(quantize(payment.amount))          # what was actually paid
    booking.addons = [*(booking.addons or []), line]
    booking.total_amount = quantize(booking.total_amount + payment.amount)
    return True
