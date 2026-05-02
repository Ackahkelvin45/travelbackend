from rest_framework import serializers

from .models import Payment


class InitializePaymentSerializer(serializers.Serializer):
    """Request body for POST /api/payments/initialize/"""

    booking_id = serializers.UUIDField(
        help_text="UUID of the Booking to pay for."
    )


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
