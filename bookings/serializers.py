from rest_framework import serializers

from packages.models import TravelPackage
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

        # Fallback for travel_date if not provided
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
        # Attach logged-in user if available (guests leave this null)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            validated_data["user"] = request.user
        return super().create(validated_data)


class BookingDetailSerializer(serializers.ModelSerializer):
    """
    Read-only response shape for GET /api/bookings/<reference>/

    Includes a flat `payment_status` field so the frontend can check
    whether to show a "Pay now" button or a "Confirmed" banner.
    """

    package_title = serializers.CharField(source="package.title", read_only=True)
    payment_status = serializers.SerializerMethodField()
    payment_reference = serializers.SerializerMethodField()

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
            "num_guests",
            "travel_date",
            "unit_price",
            "total_amount",
            "currency",
            "status",
            "payment_status",
            "payment_reference",
            "created_at",
        ]
        read_only_fields = fields

    def get_payment_status(self, obj):
        if hasattr(obj, "payment"):
            return obj.payment.status
        return None

    def get_payment_reference(self, obj):
        if hasattr(obj, "payment"):
            return obj.payment.paystack_reference
        return None
