"""JWT login serializer that blocks unverified email addresses."""

from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

NOT_VERIFIED_MESSAGE = (
    "Please verify your email address before signing in. "
    "Check your inbox for the verification link."
)


class VerifiedEmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Refuses tokens for accounts whose email hasn't been verified.

    Checks the account first: a real account with an unverified email gets a
    clear, actionable error instead of a session. Everything else falls
    through to the standard credential flow (identical errors/format).
    """

    default_error_messages = {"no_active_account": NOT_VERIFIED_MESSAGE}

    def validate(self, attrs):
        User = get_user_model()
        try:
            account = User.objects.get(
                **{self.username_field: attrs[self.username_field]}
            )
        except User.DoesNotExist:
            account = None

        if account is not None and not account.email_verified:
            raise serializers.ValidationError(
                {"detail": NOT_VERIFIED_MESSAGE, "code": "email_not_verified"}
            )

        return super().validate(attrs)
