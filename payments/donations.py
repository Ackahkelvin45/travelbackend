"""
Academy donations (Change Request v4 §7): a voluntary USD gift at checkout
(5%, 10% or 20% of the trip, added to the first payment, never pre-selected) or on the
public /donate page. Each donation is its own Paystack transaction settled to
the Academy's subaccount, so it never mixes with booking receipts. Closed
until staff switch it on with a verified subaccount.
"""

import logging
import secrets
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Donation, OpsConfig
from .money import quantize, to_subunits

logger = logging.getLogger(__name__)

CHECKOUT_PERCENTS = (5, 10, 20)  # of the trip total, added to the first payment
MIN_DONATION = Decimal("1.00")
MAX_DONATION = Decimal("50000.00")
CLOSED = "Donations open once the Academy's account is verified. Thank you for your patience."


def donations_open() -> bool:
    c = OpsConfig.get()
    return c.donations_enabled and bool(c.donation_subaccount)


def _charge_rate():
    """USD→GHS at the market rate, no margin on a gift; falls back to a USD
    package's manual rate when the feed is down (raises FxUnavailable)."""
    from packages.models import TravelPackage
    from .fx import effective_charge_rate

    manual = (TravelPackage.objects.filter(currency="USD", charge_exchange_rate__isnull=False)
              .values_list("charge_exchange_rate", flat=True).first())
    return effective_charge_rate(SimpleNamespace(
        pk="donations", currency="USD", fx_mode="live", fx_margin_percent=Decimal("0"), charge_exchange_rate=manual,
    ))


def apply_donation(donation: Donation, gateway_data: dict) -> bool:
    """Mark a verified donation received (idempotent); a gateway amount that
    differs from what was sent is flagged, never accepted. Returns True when
    this call recorded the money."""
    from .email import send_donation_receipt

    with transaction.atomic():
        d = Donation.objects.select_for_update().get(pk=donation.pk)
        if d.status == Donation.Status.SUCCESS:
            return False
        expected = to_subunits(d.charged_amount if d.charged_amount is not None else d.amount)
        currency = d.charged_currency or d.currency
        if gateway_data.get("amount") != expected or gateway_data.get("currency") != currency:
            d.needs_review = True
            d.review_note = (f"Expected {expected} {currency} subunits, gateway reported "
                             f"{gateway_data.get('amount')} {gateway_data.get('currency')}. Not recorded.")
            d.save(update_fields=["needs_review", "review_note"])
            return False
        d.status, d.paid_at, d.gateway_response = Donation.Status.SUCCESS, timezone.now(), gateway_data
        d.save(update_fields=["status", "paid_at", "gateway_response"])
        transaction.on_commit(lambda: send_donation_receipt(d))
    return True


class DonationSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    percent = serializers.ChoiceField(choices=CHECKOUT_PERCENTS, required=False, help_text="Of the booking total.")
    booking_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        if ("amount" in attrs) == ("percent" in attrs):
            raise serializers.ValidationError("Choose 5%, 10%, 20% or a custom amount.")
        if "percent" in attrs and not attrs.get("booking_id"):
            raise serializers.ValidationError({"percent": "A percentage needs a booking."})
        return attrs


class DonationCreateView(APIView):
    """POST /api/donations/ — start a donation; returns Paystack popup details."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payments"

    def post(self, request):
        from bookings.models import Booking
        from .fx import FxUnavailable
        from .paystack import initialize_transaction

        if not donations_open():
            return Response({"detail": CLOSED}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        s = DonationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        booking = None
        if data.get("booking_id"):
            booking = Booking.objects.filter(pk=data["booking_id"]).first()
            if booking is None:
                return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        amount = quantize(booking.total_amount * data["percent"] / Decimal("100")) if "percent" in data \
            else quantize(data["amount"])
        if not MIN_DONATION <= amount <= MAX_DONATION:
            return Response({"detail": f"Donations are between USD {MIN_DONATION} and USD {MAX_DONATION:,.0f}."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            rate, _ = _charge_rate()
        except FxUnavailable:
            return Response({"detail": "Donations are temporarily unavailable. Please try again later."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        subaccount = OpsConfig.get().donation_subaccount
        donation = Donation.objects.create(
            reference=f"DON-{secrets.token_hex(6).upper()}", booking=booking,
            source=Donation.Source.CHECKOUT if booking else Donation.Source.PAGE,
            donor_name=data["name"], donor_email=data["email"], amount=amount,
            charged_amount=quantize(amount * rate), charged_currency="GHS", exchange_rate=rate,
            subaccount=subaccount,
        )
        try:
            gw = initialize_transaction(
                email=donation.donor_email, amount_kobo=to_subunits(donation.charged_amount),
                reference=donation.reference, currency="GHS", subaccount=subaccount,
                callback_url=f"{settings.FRONTEND_URL.rstrip('/')}/donate/thanks",
                metadata={"kind": "academy_donation", "booking_reference": booking.reference if booking else ""},
            )
        except Exception:
            logger.exception("Paystack init failed for donation %s", donation.reference)
            donation.status = Donation.Status.FAILED
            donation.save(update_fields=["status"])
            return Response({"detail": "Payment gateway error. Please try again later."},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "reference": donation.reference, "access_code": gw.get("access_code"),
            "authorization_url": gw.get("authorization_url"), "amount": str(amount), "currency": "USD",
            "charged_amount": str(donation.charged_amount), "charged_currency": "GHS",
        }, status=status.HTTP_201_CREATED)


class DonationVerifyView(APIView):
    """GET /api/donations/verify/<reference>/ — confirm with Paystack and record."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payments"

    def get(self, request, reference):
        from .paystack import verify_transaction

        donation = Donation.objects.filter(reference=reference).first()
        if donation is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if donation.status == Donation.Status.PENDING:
            try:
                data = verify_transaction(reference)
            except Exception:
                logger.exception("Donation verify failed for %s", reference)
                data = {}
            if data.get("status") == "success":
                apply_donation(donation, data)
            elif data.get("status") in ("failed", "abandoned"):
                Donation.objects.filter(pk=donation.pk, status=Donation.Status.PENDING).update(
                    status=Donation.Status.FAILED)
            donation.refresh_from_db()
        return Response({"reference": donation.reference, "status": donation.status,
                         "amount": str(donation.amount), "currency": donation.currency})
