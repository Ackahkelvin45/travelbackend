from django.db import models
from rest_framework import serializers

from packages.models import TourDeparture, TravelPackage
from .models import Booking, BookingTraveller, Lead


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
        extra_kwargs = {"num_guests": {"min_value": 1, "max_value": 100}}

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
    line_items = serializers.SerializerMethodField()
    cure_deadline = serializers.DateTimeField(read_only=True)
    cancellation_request = serializers.SerializerMethodField()
    coordinator_name = serializers.SerializerMethodField()
    travellers = serializers.SerializerMethodField()
    donation = serializers.SerializerMethodField()
    linked_bookings = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "reference",
            "linked_bookings",
            "donation",
            "mode",
            "pay_separately",
            "whatsapp_updates",
            "coordinator_name",
            "lead_contacted_at",
            "travellers",
            "marketing_consent",
            "photo_consent",
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
            "line_items",
            "overdue_notice_sent_at",
            "cure_deadline",
            "cancellation_request",
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

    def get_travellers(self, obj):
        """Guests, rooms and experiences (component bookings). Names only —
        travel-form answers stay behind the owner-only travel endpoint."""
        names = {a.code: a.name for a in obj.package.addons.all()}
        return [
            {
                "id": str(t.id),
                "full_name": t.full_name,
                "is_lead": t.is_lead,
                "unit": t.unit.label if t.unit else None,
                "experiences": [names.get(c, c) for c in t.experiences],
                "travel_info_submitted": t.travel_info_submitted_at is not None,
                "email": t.email, "invite_status": t.invite_status, "has_account": t.user_id is not None,
                "share": str(share), "paid": str(paid), "outstanding": str(outstanding),
            }
            for t, share, paid, outstanding in obj.traveller_balances()
        ]

    def get_donation(self, obj):
        """The Academy donation received with this booking, if any (shown as its
        own line; never part of total_amount)."""
        d = obj.donations.filter(status="success").order_by("paid_at").first()
        return {"amount": str(d.amount), "percent": d.percent, "currency": d.currency,
                "reference": d.reference} if d else None

    def get_linked_bookings(self, obj):
        """Other bookings in this group (one Airbnb each, same lead)."""
        root = obj.linked_to or obj
        group = Booking.objects.filter(models.Q(pk=root.pk) | models.Q(linked_to=root)).exclude(pk=obj.pk)
        return [{"reference": b.reference, "status": b.status, "num_guests": b.num_guests}
                for b in group.exclude(status=Booking.Status.CANCELLED).order_by("created_at")]

    def get_coordinator_name(self, obj):
        """First name only — this serializer also backs the public status view."""
        return obj.coordinator.first_name if obj.coordinator else None

    def get_payment_deadline(self, obj):
        return obj.effective_payment_deadline

    def get_payment_state(self, obj):
        """Derived label — never stored. unpaid | partially_paid | fully_paid."""
        if obj.is_paid:
            return "fully_paid"
        if obj.amount_paid > 0:
            return "partially_paid"
        return "unpaid"

    def get_line_items(self, obj):
        """What the total is made of — same decomposition the receipts use,
        so dashboard, receipt and checkout always agree."""
        from payments.receipts import compute_line_items
        items = compute_line_items(obj)
        return {
            "lines": [
                {"label": l["label"], "detail": l["detail"], "amount": str(l["amount"]), "category": l["category"]}
                for l in items["lines"]
            ],
            "subtotal": str(items["component_sum"]),
            "discount": str(items["bundle_discount"]),
            "discount_note": items["discount_note"],
            "total": str(items["total"]),
        }

    def get_cancellation_request(self, obj):
        req = obj.cancellation_requests.order_by("-requested_at").first()
        if not req:
            return None
        return {
            "status": req.status,
            "requested_at": req.requested_at,
            "refund_total": (req.refund_quote or {}).get("refund_total"),
            "percent": (req.refund_quote or {}).get("percent"),
            "resolved_at": req.resolved_at,
            "staff_note": req.staff_note,
        }

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
                "disputed": p.disputed,
                "paid_at": p.paid_at,
                "created_at": p.created_at,
            }
            for p in obj.payments.all()
        ]


class BookingPublicSerializer(BookingDetailSerializer):
    """What anyone holding just the reference sees: status and money, no names,
    contact details, guest list or payment ledger (those need ownership or the
    booking's view token)."""

    class Meta(BookingDetailSerializer.Meta):
        fields = [
            "id", "reference", "mode", "pay_separately", "package_title", "package_id", "num_guests",
            "travel_date", "total_amount", "amount_paid", "balance", "payment_state", "payment_plan",
            "deposit_required", "payment_deadline", "currency", "status", "payment_status", "line_items",
        ]
        read_only_fields = fields


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


class ConfigurableCheckoutSerializer(serializers.Serializer):
    """POST /api/bookings/checkout/configurable/ — the core_plus_addons flow:
    a mandatory base + a set of chosen add-on codes. Prices are entirely
    server-computed; expected_total guards a stale cart (409 + fresh quote)."""

    package_id = serializers.UUIDField()
    num_guests = serializers.IntegerField(min_value=1, max_value=100, default=1)
    selected_addon_codes = serializers.ListField(
        child=serializers.CharField(), default=list,
        help_text="Add-on codes the customer chose, e.g. ['hotel_mid', 'enzo_vip'].",
    )
    payment_plan = serializers.ChoiceField(choices=["full", "installment"], default="full")
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country = serializers.CharField(max_length=100, required=False, allow_blank=True)
    special_requests = serializers.CharField(required=False, allow_blank=True)
    accepted_policies = serializers.ListField(child=serializers.CharField(), default=list)
    expected_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False,
        help_text="The total the customer was shown. Mismatch returns 409 with a fresh quote.",
    )


# ── Component pricing (Change Request v4) ─────────────────────────────────────

class _TravellerInput(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    experiences = serializers.ListField(child=serializers.CharField(max_length=60), default=list, max_length=20)
    adult = serializers.BooleanField(default=False, help_text="Guest confirms they are 18 or over (adults-only items).")


class _RoomInput(serializers.Serializer):
    stay_type = serializers.CharField(max_length=60)
    occupants = serializers.ListField(child=serializers.IntegerField(min_value=0), max_length=10)


class ComponentQuoteSerializer(serializers.Serializer):
    """Travellers by list index; rooms list their occupants by that index."""
    package_id = serializers.UUIDField()
    mode = serializers.ChoiceField(choices=Booking.Mode.values)
    travellers = _TravellerInput(many=True)
    rooms = _RoomInput(many=True, default=list)
    payment_plan = serializers.ChoiceField(choices=["full", "installment"], default="full")
    hold_token = serializers.CharField(max_length=40, required=False, allow_blank=True)

    def validate_travellers(self, value):
        if not 1 <= len(value) <= 20:
            raise serializers.ValidationError("Between 1 and 20 travellers per booking.")
        return value


class HoldSerializer(serializers.Serializer):
    package_id = serializers.UUIDField()
    stay_types = serializers.ListField(child=serializers.CharField(max_length=60), min_length=1, max_length=10)
    replace_token = serializers.CharField(max_length=40, required=False, allow_blank=True)


class ComponentCheckoutSerializer(ComponentQuoteSerializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country = serializers.CharField(max_length=100, required=False, allow_blank=True)
    special_requests = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    accepted_policies = serializers.ListField(child=serializers.CharField(), default=list)
    marketing_consent = serializers.BooleanField(default=False)
    pay_separately = serializers.BooleanField(default=False, help_text="Group: each member pays their own share.")
    whatsapp_updates = serializers.BooleanField(default=False, help_text="Opt in to confirmations on WhatsApp.")
    expected_total = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    linked_to = serializers.CharField(
        max_length=20, required=False, allow_blank=True,
        help_text="Reference of the group's first booking (an extra Airbnb for a big group; same lead email).",
    )


class TravelInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingTraveller
        fields = [
            "id", "full_name", "is_lead", "arrival_at", "arrival_flight", "departure_at",
            "departure_flight", "needs_transfer", "emergency_contact", "emergency_phone",
            "dietary", "accessibility", "travel_info_submitted_at",
        ]
        read_only_fields = ["id", "full_name", "is_lead", "travel_info_submitted_at"]


class LeadSerializer(serializers.ModelSerializer):
    """Public lead capture. A saved quote sends its trip-builder ``selection``
    and the server prices it — a client-supplied total is never stored."""
    selection = ComponentQuoteSerializer(required=False, write_only=True)

    class Meta:
        model = Lead
        fields = ["name", "email", "phone", "source", "message", "marketing_consent", "package",
                  "referral_code", "selection"]
        # Optional on the model (WhatsApp leads), still required from the site.
        extra_kwargs = {"email": {"required": True, "allow_blank": False}}

    def validate_source(self, value):
        if value not in (Lead.Source.CHAT, Lead.Source.SAVED_QUOTE, Lead.Source.CONTACT):
            raise serializers.ValidationError("Unsupported source.")
        return value


class AssistantTurnSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "assistant"])
    content = serializers.CharField(max_length=2000)


class AssistantSerializer(serializers.Serializer):
    messages = AssistantTurnSerializer(many=True, min_length=1, max_length=30)

    def validate_messages(self, value):
        if value[0]["role"] != "user" or value[-1]["role"] != "user":
            raise serializers.ValidationError("The conversation must start and end with the guest.")
        return value


class GuestDetailsSerializer(TravelInfoSerializer):
    """What an invited guest completes from their invite link."""
    full_name = serializers.CharField(max_length=200)

    class Meta(TravelInfoSerializer.Meta):
        fields = [*TravelInfoSerializer.Meta.fields, "email", "phone", "details_completed_at"]
        read_only_fields = ["id", "is_lead", "email", "travel_info_submitted_at", "details_completed_at"]

    def validate_full_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Please give your full name.")
        return value.strip()
