from rest_framework import serializers

from .models import Payment


class InitializePaymentSerializer(serializers.Serializer):
    """Request body for POST /api/payments/initialize/"""

    booking_id = serializers.UUIDField(
        help_text="UUID of the Booking to pay for."
    )
    intent = serializers.ChoiceField(
        choices=["balance", "deposit", "custom"],
        default="balance",
        help_text=(
            "balance = pay everything outstanding (default). "
            "deposit = pay the outstanding part of the installment deposit. "
            "custom = pay `amount` (server-clamped to the outstanding balance)."
        ),
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False,
        help_text="Required for intent=custom. Ignored otherwise.",
    )
    channel = serializers.ChoiceField(
        choices=["card", "momo"],
        default="card",
        help_text=(
            "card = hosted checkout / Inline popup (authorization_url + access_code). "
            "momo = Mobile Money: the approve-prompt is pushed straight to the "
            "buyer's phone (MTN/AirtelTigo) or a mobile-money-only hosted page "
            "is returned (Telecel)."
        ),
    )
    momo_phone = serializers.CharField(
        max_length=20, required=False,
        help_text="Mobile Money number to charge. Required for channel=momo.",
    )
    momo_provider = serializers.ChoiceField(
        choices=["mtn", "atl", "vod"],
        required=False,
        help_text="Buyer-chosen network (never inferred from the phone prefix). "
                  "Required for channel=momo.",
    )

    def validate(self, attrs):
        if attrs.get("intent") == "custom":
            amount = attrs.get("amount")
            if amount is None or amount <= 0:
                raise serializers.ValidationError(
                    {"amount": "A positive amount is required for a custom payment."}
                )
        if attrs.get("channel") == "momo":
            if not attrs.get("momo_phone"):
                raise serializers.ValidationError(
                    {"momo_phone": "The Mobile Money number is required."}
                )
            if not attrs.get("momo_provider"):
                raise serializers.ValidationError(
                    {"momo_provider": "Choose the Mobile Money network."}
                )
        return attrs


class PaymentSerializer(serializers.ModelSerializer):
    """Read-only payment detail returned after verify."""

    class Meta:
        model = Payment
        fields = [
            "id",
            "paystack_reference",
            "paystack_access_code",
            "paystack_authorization_url",
            "amount",
            "currency",
            "status",
            "paid_at",
            "created_at",
        ]
        read_only_fields = fields
