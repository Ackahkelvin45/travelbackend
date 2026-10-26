"""
payments/services.py
--------------------
The ONLY code allowed to apply money to a booking.

Both the verify endpoint and the Paystack webhook converge here, as does
offline (bank transfer) payment recording from the admin. All state changes
happen inside one transaction holding a row lock on the BOOKING (the shared
resource — locking payment rows would not stop two concurrent top-ups).
Lock ordering is fixed: Booking first, then Payment. Never lock in the other
order anywhere in the codebase.
"""

import logging
import uuid
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from bookings.models import Booking
from .alerts import alert_admin
from .models import Donation, Payment
from .money import FULLY_PAID_TOLERANCE, quantize, to_subunits

logger = logging.getLogger(__name__)



def _payment_rows(p) -> list:
    return [("Payment reference", p.paystack_reference), ("Payment amount", f"{p.currency} {p.amount:,.2f}"),
            ("Charged by Paystack", f"{p.charged_currency or 'GHS'} {p.charged_amount:,.2f}" if p.charged_amount else ""),
            ("Exchange rate", str(p.exchange_rate) if p.exchange_rate else "")]

def generate_payment_reference(booking) -> str:
    """
    Fresh, unique gateway reference per payment attempt.

    Paystack references are single-use once a charge attempt exists, so a
    declined card must never lead to reusing a reference (that is exactly the
    bug that used to strand bookings). Format keeps the booking reference
    visible for support/reconciliation.
    """
    for _ in range(10):
        ref = f"AZT-PAY-{booking.reference}-{uuid.uuid4().hex[:6].upper()}"
        if not Payment.objects.filter(paystack_reference=ref).exists():
            return ref
    raise RuntimeError("Could not generate a unique payment reference.")


def _confirmation_threshold(booking):
    """
    Amount that must be received for a pending booking to become confirmed.

    Full-payment bookings: the total. Installment bookings (option-based,
    added in the flagship-tour work): the snapshotted deposit requirement.
    """
    deposit_required = getattr(booking, "deposit_required", None)
    payment_plan = getattr(booking, "payment_plan", None)
    if deposit_required and (payment_plan == "installment" or getattr(booking, "pay_separately", False)):
        return min(deposit_required, booking.total_amount)
    return booking.total_amount


@dataclass
class ApplyResult:
    payment: Payment
    booking: Booking
    applied: bool = False          # this call transitioned the payment to success
    already_applied: bool = False  # idempotent no-op (dup webhook / verify race)
    promoted: bool = False         # booking moved pending -> confirmed
    fully_paid: bool = False
    problems: list = field(default_factory=list)


def apply_successful_payment(payment: Payment, gateway_data: dict | None) -> ApplyResult:
    """
    Idempotently apply a successful charge to its booking.

    ``gateway_data`` is Paystack's verify/webhook ``data`` payload. Pass
    ``None`` only for trusted manual entries (offline bank transfers recorded
    by staff) — that skips gateway amount verification, nothing else.

    Guarantees:
      - amount & currency verified against the gateway before anything changes
      - idempotent per payment (safe under verify-vs-webhook races)
      - never confirms a cancelled/completed booking (flags for review instead)
      - overpayment is recorded explicitly, never silently absorbed
      - emails fire only after commit, and only on state transitions
    """
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)

        result = ApplyResult(payment=payment, booking=booking)

        if payment.status == Payment.Status.SUCCESS:
            result.already_applied = True
            return result

        # ── 1. Verify what the gateway actually charged ──────────────────────
        if gateway_data is not None:
            gw_amount = gateway_data.get("amount")
            gw_currency = gateway_data.get("currency")
            # The gateway charged the converted amount for non-GHS bookings —
            # verify against what was actually sent, not the ledger amount.
            expected_amount = payment.charged_amount if payment.charged_amount is not None else payment.amount
            expected_currency = payment.charged_currency or payment.currency
            expected_subunits = to_subunits(expected_amount)
            donation = Donation.objects.filter(payment=payment).first()
            if donation:  # charged together, split to the Academy at settlement
                expected_subunits += to_subunits(
                    donation.charged_amount if donation.charged_amount is not None else donation.amount)
            if gw_amount != expected_subunits or gw_currency != expected_currency:
                payment.gateway_response = gateway_data
                payment.needs_review = True
                payment.review_note = (
                    f"Gateway mismatch: expected {expected_subunits} {expected_currency} "
                    f"subunits, gateway reported {gw_amount} {gw_currency}. Not applied."
                )
                payment.save(update_fields=["gateway_response", "needs_review", "review_note", "updated_at"])
                logger.error(
                    "Payment %s amount/currency mismatch (expected %s %s, got %s %s) — flagged for review.",
                    payment.pk, expected_subunits, expected_currency, gw_amount, gw_currency,
                )
                transaction.on_commit(
                    lambda pk=payment.pk, note=payment.review_note, p=payment:
                        alert_admin(f"gateway-mismatch-{pk}", "Payment gateway amount mismatch", note,
                                    rows=_payment_rows(p), booking=p.booking, link_obj=p)
                )
                result.problems.append("gateway_mismatch")
                return result
            payment.gateway_response = gateway_data
            if donation and donation.status != Donation.Status.SUCCESS:
                # The split already settled the Academy's share, whatever happens to the booking.
                donation.status, donation.paid_at, donation.gateway_response = (
                    Donation.Status.SUCCESS, timezone.now(), gateway_data)
                donation.save(update_fields=["status", "paid_at", "gateway_response"])
                from .email import send_donation_receipt
                transaction.on_commit(lambda d=donation: send_donation_receipt(d))

        # ── 2. Terminal-state guard ──────────────────────────────────────────
        # Money arrived for a dead booking (late webhook after cancellation,
        # payment racing an admin action). Record the money, never promote,
        # flag for manual refund.
        if booking.status in (Booking.Status.CANCELLED, Booking.Status.COMPLETED):
            payment.status = Payment.Status.SUCCESS
            payment.paid_at = timezone.now()
            payment.needs_review = True
            payment.review_note = (
                f"Charge succeeded but booking is {booking.status}. "
                "Not applied to booking totals — likely needs a refund."
            )
            payment.save()
            logger.warning(
                "Payment %s succeeded against %s booking %s — flagged for review.",
                payment.pk, booking.status, booking.reference,
            )
            transaction.on_commit(
                lambda pk=payment.pk, note=payment.review_note, p=payment:
                    alert_admin(f"terminal-payment-{pk}", "Money received on a closed booking", note,
                                rows=_payment_rows(p), booking=p.booking, link_obj=p)
            )
            result.applied = True
            result.problems.append("terminal_booking")
            return result

        # ── 3. Apply ─────────────────────────────────────────────────────────
        payment.status = Payment.Status.SUCCESS
        payment.paid_at = timezone.now()

        # Recompute the cache from the ledger while holding the booking lock —
        # this payment's row is already saved below within the same transaction.
        payment.save()
        ledger_total = (
            Payment.objects.filter(booking=booking, status=Payment.Status.SUCCESS)
            .aggregate(total=Sum("amount"))["total"]
        ) or 0
        booking.amount_paid = quantize(ledger_total)

        # ── 3b. An added experience joins the booking now it's paid ─────────
        addon_added = False
        if payment.purpose == Payment.Purpose.ADDON:
            from bookings.services import apply_paid_addon
            addon_added = apply_paid_addon(booking, payment)

        # ── 4. Overpayment guard ─────────────────────────────────────────────
        overpaid = booking.amount_paid - booking.total_amount
        if overpaid > FULLY_PAID_TOLERANCE:
            payment.needs_review = True
            payment.review_note = (
                f"Booking overpaid by {overpaid} {booking.currency} after this "
                "payment (concurrent payments?). Excess should be refunded."
            )
            payment.save(update_fields=["needs_review", "review_note", "updated_at"])
            logger.warning(
                "Booking %s overpaid by %s %s.", booking.reference, overpaid, booking.currency
            )
            transaction.on_commit(
                lambda ref=booking.reference, note=payment.review_note, p=payment:
                    alert_admin(f"overpaid-{ref}", f"Booking {ref} overpaid", note,
                                rows=_payment_rows(p), booking=p.booking, link_obj=p)
            )
            result.problems.append("overpaid")

        # ── 5. Promote booking when the threshold is met ─────────────────────
        threshold = _confirmation_threshold(booking)
        if (
            booking.status == Booking.Status.PENDING
            and booking.amount_paid >= threshold - FULLY_PAID_TOLERANCE
        ):
            booking.status = Booking.Status.CONFIRMED
            result.promoted = True
            # Paid: the guest's open leads are won, which stops sales reminders.
            from bookings.models import Lead
            Lead.objects.filter(email__iexact=booking.email).exclude(
                stage__in=[Lead.Stage.WON, Lead.Stage.LOST],
            ).update(stage=Lead.Stage.WON, booking=booking)

        booking.save(update_fields=["amount_paid", "status", "updated_at"]
                     + (["addons", "total_amount"] if addon_added else []))

        # ── 5b. Component bookings: required payment in → held rooms become sold
        # A room whose checkout window lapsed may have been resold; flag it.
        rooms_expected = len((booking.quote_snapshot or {}).get("rooms", []))
        if rooms_expected and booking.status == Booking.Status.CONFIRMED:
            from packages.models import RoomUnit
            rooms_kept = RoomUnit.objects.filter(booking=booking).update(held_until=None)
            if rooms_kept < rooms_expected:
                payment.needs_review = True
                payment.review_note = (
                    f"Booking {booking.reference} paid but holds {rooms_kept} of "
                    f"{rooms_expected} rooms — the unpaid hold lapsed. Reassign a room."
                )
                payment.save(update_fields=["needs_review", "review_note", "updated_at"])
                transaction.on_commit(
                    lambda ref=booking.reference, note=payment.review_note, b=booking:
                        alert_admin(f"room-lost-{ref}", f"Booking {ref} lost a room", note, booking=b)
                )
                result.problems.append("room_lost")

        result.applied = True
        result.fully_paid = booking.is_paid

        # ── 6. Notify after commit only ──────────────────────────────────────
        # Promotion → full confirmation email (once, ever). Any other applied
        # payment (top-ups, balance) → a receipt, which announces "fully paid"
        # when the balance reaches zero.
        send_confirmation = result.promoted

        def _send(booking_id=booking.pk, payment_id=payment.pk, confirmation=send_confirmation):
            from .email import send_booking_confirmation, send_payment_receipt
            try:
                b = Booking.objects.select_related("package").get(pk=booking_id)
                p = Payment.objects.get(pk=payment_id)
                if confirmation:
                    send_booking_confirmation(b, p)
                else:
                    send_payment_receipt(b, p)
            except Exception:
                logger.exception("Post-commit email failed for booking %s", booking_id)
            try:
                from .whatsapp_notify import send_payment_whatsapp
                send_payment_whatsapp(Booking.objects.get(pk=booking_id))
            except Exception:
                logger.exception("Post-commit WhatsApp failed for booking %s", booking_id)

        if "terminal_booking" not in result.problems:
            transaction.on_commit(_send)

        return result


def mark_payment_unsuccessful(payment: Payment, gateway_status: str, gateway_data: dict | None = None) -> Payment:
    """Record a failed/abandoned gateway outcome. Idempotent; never downgrades success."""
    status_map = {
        "failed": Payment.Status.FAILED,
        "abandoned": Payment.Status.ABANDONED,
    }
    new_status = status_map.get(gateway_status)
    if new_status is None:
        return payment

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == Payment.Status.SUCCESS:
            return payment  # webhook/verify race: success always wins
        was = payment.status
        payment.status = new_status
        if gateway_data is not None:
            payment.gateway_response = gateway_data
        payment.save()
        if new_status == Payment.Status.FAILED and was != Payment.Status.FAILED:
            from .email import send_payment_failed
            transaction.on_commit(lambda: send_payment_failed(payment))
    return payment


def record_offline_payment(booking, *, amount, method, note, purpose=None) -> ApplyResult:
    """
    Staff-recorded payment (bank transfer etc.). Runs through the exact same
    application path as gateway payments so confirmation, receipts and cached
    totals behave identically regardless of channel.
    """
    amount = quantize(amount)
    if amount <= 0:
        raise ValueError("Offline payment amount must be positive.")

    payment = Payment.objects.create(
        booking=booking,
        amount=amount,
        currency=booking.currency,
        method=method,
        purpose=purpose or Payment.Purpose.INSTALLMENT,
        note=note,
    )
    return apply_successful_payment(payment, gateway_data=None)
