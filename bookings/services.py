"""
bookings/services.py
--------------------
Booking creation for the option-based (flagship tour) flow.

Everything commercial is computed by bookings.pricing and snapshotted here,
inside one transaction that also records the policy acceptances — a booking
without its acceptances never exists.
"""

import logging

from django.db import transaction

from .models import Booking, PolicyAcceptance, PolicyDocument
from .pricing import QuoteError, compute_quote

logger = logging.getLogger(__name__)


class PolicyAcceptanceRequired(Exception):
    def __init__(self, missing_types):
        self.missing_types = missing_types
        super().__init__(f"Missing policy acceptances: {', '.join(missing_types)}")


def required_policy_documents():
    """Every currently-published policy version must be accepted at checkout."""
    return list(PolicyDocument.objects.filter(is_current=True, published_at__isnull=False))


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


class IllegalTransition(Exception):
    pass


def cancel_booking(booking, *, reason: str, refund_reason: str = "") -> Booking:
    """
    The one legal way to cancel a booking. Sets the cancellation facts and,
    when money was paid, materializes the computed refund as pending legs for
    the operator to execute.

    ``reason`` is a Booking.CancellationReason value — customer-requested,
    expired, overdue forfeit, or admin.
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
            )

    logger.info(
        "Booking %s cancelled (%s), %s pending refund leg(s) created.",
        booking.reference, reason, len(legs),
    )
    return booking
