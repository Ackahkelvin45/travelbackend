"""
payments/refunds.py
-------------------
Backend source of truth for cancellation refunds.

``compute_refund`` is a pure function over the booking's SNAPSHOTTED refund
tiers and the payment ledger — admin edits to live policy never change an
existing customer's entitlement. Because gateway refunds are per-transaction,
the computation returns an allocation plan (per-payment legs), not just a
number.

Execution is manual in v1 (Paystack dashboard / bank transfer): pending
Refund rows are the computed decision; ``mark_refund_processed`` records the
execution and updates the booking's cached totals under the booking lock.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Payment, Refund
from .money import quantize

logger = logging.getLogger(__name__)


def _tier_for(days_before_departure: int, tiers: list) -> dict:
    """Most-generous tier whose min_days threshold is met; 0% if none."""
    applicable = [t for t in tiers if days_before_departure >= int(t["min_days"])]
    if not applicable:
        return {"min_days": 0, "percent": 0}
    return max(applicable, key=lambda t: int(t["min_days"]))


def compute_refund(booking, at=None, *, full: bool = False) -> dict:
    """
    What is owed back if this booking is cancelled at ``at``.

    Rules (in order):
      1. net paid = successful payments − already-processed refunds
      2. non-refundable add-ons (visa etc., from the booking snapshot) are
         excluded before the percentage
      3. the percentage:
           ``full=True``  → 100% — organizer cancellation, bands never apply
           guest arrived  → 0%   — arrival/start rule (booking.arrived_at <= at)
           otherwise      → the snapshotted tier for days-before-departure
      4. the result is decomposed into per-payment legs (newest charge first),
         each capped by that charge's un-refunded remainder
    """
    at = at or timezone.now()

    tiers = booking.refund_tiers_snapshot or booking.package.refund_tiers or []
    days_before = (booking.travel_date - at.date()).days
    if full:
        tier = {"min_days": None, "percent": 100, "basis": "organizer_cancellation"}
    elif booking.arrived_at and booking.arrived_at <= at:
        tier = {"min_days": None, "percent": 0, "basis": "arrived_or_started"}
    else:
        tier = _tier_for(days_before, tiers) if tiers else {"min_days": 0, "percent": 0}

    already_refunded = quantize(
        booking.refunds.filter(status=Refund.Status.PROCESSED).aggregate(t=Sum("amount"))["t"] or 0
    )
    paid = quantize(
        booking.payments.filter(status=Payment.Status.SUCCESS).aggregate(t=Sum("amount"))["t"] or 0
    )
    net_paid = quantize(paid - already_refunded)

    non_refundable = quantize(sum(
        Decimal(str(line.get("line_total", "0")))
        for line in (booking.addons or [])
        if not line.get("refundable", True)
    ))

    refundable_base = max(net_paid - non_refundable, Decimal("0.00"))
    percent = Decimal(str(tier["percent"]))
    refund_total = quantize(refundable_base * percent / 100)

    # ── Allocation: newest charge first, capped per charge ───────────────────
    allocation = []
    remaining = refund_total
    if remaining > 0:
        payments = list(
            booking.payments.filter(status=Payment.Status.SUCCESS).order_by("-paid_at", "-created_at")
        )
        for payment in payments:
            if remaining <= 0:
                break
            if payment.disputed:
                # Chargeback open on this charge: the bank may already be
                # reversing it — never refund the same money twice.
                continue
            refunded_on_payment = quantize(
                payment.refunds.filter(status=Refund.Status.PROCESSED).aggregate(t=Sum("amount"))["t"] or 0
            )
            capacity = quantize(payment.amount - refunded_on_payment)
            if capacity <= 0:
                continue
            leg = min(capacity, remaining)
            # Gateway refunds are executed in the CHARGED currency (GHS for
            # USD bookings) — give the operator the exact proportional GHS
            # figure so nothing is left to mental arithmetic.
            gateway_amount = None
            if payment.charged_amount is not None and payment.amount:
                gateway_amount = quantize(payment.charged_amount * leg / payment.amount)
            allocation.append({
                "payment": payment,
                "amount": leg,
                "gateway_refund_amount": gateway_amount,
                "gateway_currency": payment.charged_currency,
            })
            remaining = quantize(remaining - leg)

    breakdown = {
        "computed_at": at.isoformat(),
        "departure_date": booking.travel_date.isoformat(),
        "days_before_departure": days_before,
        "tier_applied": tier,
        "amount_paid": str(paid),
        "previously_refunded": str(already_refunded),
        "net_paid": str(net_paid),
        "non_refundable_components": str(non_refundable),
        "refundable_base": str(refundable_base),
        "percent": str(percent),
        "refund_total": str(refund_total),
        "unallocatable": str(remaining),
    }
    return {"refund_total": refund_total, "breakdown": breakdown, "allocation": allocation}


def create_fixed_refund(booking, amount, *, reason: str, breakdown: dict | None = None) -> list:
    """Owe the guest a specific amount (e.g. an over-payment after an add-on is
    removed): decompose it into pending legs newest-charge-first, skipping
    disputed payments, exactly like a policy refund."""
    amount = quantize(amount)
    legs = []
    if amount <= 0:
        return legs
    remaining = amount
    with transaction.atomic():
        for payment in booking.payments.filter(status=Payment.Status.SUCCESS).order_by("-paid_at", "-created_at"):
            if remaining <= 0:
                break
            if payment.disputed:
                continue
            refunded = quantize(payment.refunds.filter(status=Refund.Status.PROCESSED).aggregate(t=Sum("amount"))["t"] or 0)
            capacity = quantize(payment.amount - refunded)
            if capacity <= 0:
                continue
            leg = min(capacity, remaining)
            info = dict(breakdown or {})
            if payment.charged_amount is not None and payment.amount:
                gw = quantize(payment.charged_amount * leg / payment.amount)
                info["execute_on_gateway"] = (
                    f"Refund {payment.charged_currency} {gw} on Paystack transaction "
                    f"{payment.paystack_reference} (= {booking.currency} {leg} of the ledger)."
                )
            legs.append(Refund.objects.create(
                booking=booking, payment=payment, amount=leg, currency=booking.currency,
                reason=reason, breakdown=info,
            ))
            remaining = quantize(remaining - leg)
    return legs


def create_pending_refunds(booking, *, reason: str, at=None, full: bool = False) -> list:
    """Materialize the computed allocation as pending Refund legs (idempotent-ish:
    refuses when pending legs already exist so a double admin click can't
    double the payout)."""
    if booking.refunds.filter(status=Refund.Status.PENDING).exists():
        raise ValueError(
            "This booking already has pending refunds — process or reject them first."
        )

    computed = compute_refund(booking, at=at, full=full)
    legs = []
    with transaction.atomic():
        for item in computed["allocation"]:
            breakdown = dict(computed["breakdown"])
            if item.get("gateway_refund_amount") is not None:
                breakdown["execute_on_gateway"] = (
                    f"Refund {item['gateway_currency']} {item['gateway_refund_amount']} "
                    f"on Paystack transaction {item['payment'].paystack_reference} "
                    f"(= {booking.currency} {item['amount']} of the ledger)."
                )
            legs.append(Refund.objects.create(
                booking=booking,
                payment=item["payment"],
                amount=item["amount"],
                currency=booking.currency,
                reason=reason,
                breakdown=breakdown,
            ))
    logger.info(
        "Computed refund for %s: total=%s legs=%s",
        booking.reference, computed["refund_total"], len(legs),
    )
    return legs


def mark_refund_processed(refund: Refund, *, by_user, execution_note="", external_reference="") -> Refund:
    """Record that the money actually went back, and update the booking cache —
    the only writer of amount_refunded, under the booking lock."""
    from bookings.models import Booking

    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=refund.booking_id)
        refund = Refund.objects.select_for_update().get(pk=refund.pk)

        if refund.status == Refund.Status.PROCESSED:
            return refund  # idempotent
        if refund.status == Refund.Status.REJECTED:
            raise ValueError("A rejected refund cannot be processed.")
        if refund.payment is None:
            raise ValueError("A refund must reference the payment it returns.")
        if refund.payment.disputed:
            raise ValueError(
                "This payment has an open chargeback — resolve the dispute before refunding."
            )

        refund.status = Refund.Status.PROCESSED
        refund.processed_by = by_user
        refund.processed_at = timezone.now()
        refund.execution_note = execution_note or refund.execution_note
        refund.external_reference = external_reference or refund.external_reference
        refund.save()

        booking.amount_refunded = quantize(
            booking.refunds.filter(status=Refund.Status.PROCESSED).aggregate(t=Sum("amount"))["t"] or 0
        )
        booking.save(update_fields=["amount_refunded", "updated_at"])

        # A fully-refunded charge is marked on the payment ledger too.
        payment = refund.payment
        refunded_on_payment = quantize(
            payment.refunds.filter(status=Refund.Status.PROCESSED).aggregate(t=Sum("amount"))["t"] or 0
        )
        if refunded_on_payment >= payment.amount:
            Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.REFUNDED)

        # Refund Policy: tell the guest the amount, date and reference once the
        # money is on its way. After commit so a rollback never emails.
        from .email import send_refund_processed
        transaction.on_commit(lambda: send_refund_processed(refund))

    return refund
