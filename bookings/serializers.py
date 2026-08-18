from rest_framework import serializers

from packages.models import TourDeparture, TravelPackage
from .models import Booking


# Map frontend tier name → model field name
PRICE_TIER_MAP = {
    "shared": "price_shared",
    "private": "price_private",
    "vip": "price_vip",
}


class CreateBookingSerializer(serializers.ModelSerializer):
    """
    POST /api/bookings/

    The frontend sends:
      - package_id   : UUID of the TravelPackage
      - price_tier   : "shared" | "private" | "vip"
      - guest details: first_name, last_name, email, phone, country
      - travel_date  : ISO date string
      - num_guests   : integer (default 1)
      - special_requests (optional)

    The serializer resolves unit_price + total_amount server-side so the
    frontend can never manipulate the price.
    """

    package_id = serializers.UUIDField(write_only=True)
    price_tier = serializers.ChoiceField(
        choices=list(PRICE_TIER_MAP.keys()),
        default="shared",
        write_only=True,
        help_text="Which pricing option: shared, private, or vip.",
    )

    travel_date = serializers.DateField(
        required=False,
        help_text="Date of travel. If omitted, defaults to package available_from or today."
    )

    class Meta:
        model = Booking
        fields = [
            "package_id",
            "price_tier",
            "first_name",
            "last_name",
            "email",
            "phone",
            "country",
            "num_guests",
            "travel_date",
            "special_requests",
        ]

    def validate(self, attrs):
        package_id = attrs.pop("package_id")
        price_tier = attrs.pop("price_tier")

        # Resolve package
        try:
            package = TravelPackage.objects.get(id=package_id, is_active=True)
        except TravelPackage.DoesNotExist:
            raise serializers.ValidationError(
                {"package_id": "Package not found or is no longer available."}
            )

        # ── Day-tour departures ───────────────────────────────────────────────
        # If the package runs on scheduled departure dates, the booking must
        # land on one of them (and it must still have seats). The chosen date
        # becomes travel_date; the actual seat reservation happens under a row
        # lock in create().
        active_departures = list(package.departures.filter(is_active=True))
        if active_departures:
            chosen = attrs.get("travel_date")
            valid_dates = {d.date for d in active_departures}
            if chosen is None:
                raise serializers.ValidationError(
                    {"travel_date": "Please choose a departure date for this tour."}
                )
            if chosen not in valid_dates:
                raise serializers.ValidationError(
                    {"travel_date": "That date is not an available departure for this tour."}
                )
            departure = next(d for d in active_departures if d.date == chosen)
            if departure.is_full:
                raise serializers.ValidationError(
                    {"travel_date": "This departure is fully booked. Please choose another date."}
                )
            attrs["_departure_id"] = departure.id

        # Fallback for travel_date if not provided (non-departure packages only)
        if "travel_date" not in attrs:
            from django.utils import timezone
            attrs["travel_date"] = package.available_from or timezone.now().date()

        # Resolve unit price from chosen tier
        price_field = PRICE_TIER_MAP[price_tier]
        unit_price = getattr(package, price_field)
        if unit_price is None:
            raise serializers.ValidationError(
                {
                    "price_tier": (
                        f"The '{price_tier}' pricing option is not available "
                        "for this package."
                    )
                }
            )

        num_guests = attrs.get("num_guests", 1)

        # Inject computed fields — passed straight through to Booking.objects.create()
        attrs["package"] = package
        attrs["unit_price"] = unit_price
        attrs["total_amount"] = unit_price * num_guests
        attrs["currency"] = package.currency
        return attrs

    def create(self, validated_data):
        from django.db import transaction

        departure_id = validated_data.pop("_departure_id", None)
        num_guests = validated_data.get("num_guests", 1)

        # Attach logged-in user if available (guests leave this null)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            validated_data["user"] = request.user

        if departure_id is None:
            return super().create(validated_data)

        # Reserve seats atomically so a departure can never oversell under
        # concurrent bookings (lock the departure row first, then create).
        with transaction.atomic():
            departure = (
                TourDeparture.objects.select_for_update().get(pk=departure_id)
            )
            if departure.capacity is not None and departure.seats_taken + num_guests > departure.capacity:
                raise serializers.ValidationError(
                    {"travel_date": "Not enough seats left on this departure for that many guests."}
                )
            booking = super().create(validated_data)
            departure.seats_taken += num_guests
            departure.save(update_fields=["seats_taken"])
        return booking


class BookingDetailSerializer(serializers.ModelSerializer):
    """
    Read-only response shape for GET /api/bookings/<reference>/

    Includes a flat `payment_status` field so the frontend can check
    whether to show a "Pay now" button or a "Confirmed" banner.
    """

    package_title = serializers.CharField(source="package.title", read_only=True)
    package_id = serializers.UUIDField(source="package.id", read_only=True)
    payment_deadline = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    payment_reference = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    payment_state = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "reference",
            "first_name",
            "last_name",
            "email",
            "phone",
            "country",
            "package_title",
            "package_id",
            "num_guests",
            "travel_date",
            "unit_price",
            "total_amount",
            "amount_paid",
            "amount_refunded",
            "balance",
            "payment_state",
            "payment_plan",
            "deposit_required",
            "payment_deadline",
            "early_bird_applied",
            "early_bird_discount",
            "addons",
            "currency",
            "status",
            "payment_status",
            "payment_reference",
            "payments",
            "created_at",
        ]
        read_only_fields = fields

    # ── Latest-payment fields, kept for backwards compatibility with the
    #    deployed frontend (which predates the multi-payment ledger). ──
    def _latest_payment(self, obj):
        payments = list(obj.payments.all())
        return payments[0] if payments else None  # Payment.Meta orders -created_at

    def get_payment_status(self, obj):
        payment = self._latest_payment(obj)
        return payment.status if payment else None

    def get_payment_reference(self, obj):
        payment = self._latest_payment(obj)
        return payment.paystack_reference if payment else None

    def get_balance(self, obj):
        return str(obj.balance)

    def get_payment_deadline(self, obj):
        return obj.effective_payment_deadline

    def get_payment_state(self, obj):
        """Derived label — never stored. unpaid | partially_paid | fully_paid."""
        if obj.is_paid:
            return "fully_paid"
        if obj.amount_paid > 0:
            return "partially_paid"
        return "unpaid"

    def get_payments(self, obj):
        return [
            {
                "id": str(p.id),
                "reference": p.paystack_reference,
                "amount": str(p.amount),
                "currency": p.currency,
                "charged_amount": str(p.charged_amount) if p.charged_amount is not None else None,
                "charged_currency": p.charged_currency,
                "purpose": p.purpose,
                "method": p.method,
                "status": p.status,
                "paid_at": p.paid_at,
                "created_at": p.created_at,
            }
            for p in obj.payments.all()
        ]


class CheckoutSerializer(serializers.Serializer):
    """
    POST /api/bookings/checkout/ — the option-based (flagship tour) create
    endpoint. Prices are entirely server-computed; `expected_total` exists
    only so a stale checkout page (e.g. early bird expired mid-session) gets
    a 409 with fresh numbers instead of a silent price change.
    """

    option_id = serializers.UUIDField()
    visa = serializers.BooleanField(default=False)
    payment_plan = serializers.ChoiceField(
        choices=["full", "installment"], default="full"
    )
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country = serializers.CharField(max_length=100, required=False, allow_blank=True)
    special_requests = serializers.CharField(required=False, allow_blank=True)
    accepted_policies = serializers.ListField(
        child=serializers.CharField(), default=list,
        help_text="Policy types the customer ticked, e.g. ['terms', 'refund'].",
    )
    expected_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False,
        help_text="The total the customer was shown. Mismatch returns 409 with a fresh quote.",
    )
