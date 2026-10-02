from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .emails import (
    make_verification_token,
    read_verification_token,
    send_verification_email,
    send_welcome_email,
)
from .models import User
from .password_validation import password_rule_results
from .serializers import UserRegistrationSerializer, UserProfileSerializer


def _verification_url(request, user) -> str:
    """Absolute link to the verify endpoint, built from the incoming request so
    it works behind the proxy without a hardcoded backend URL."""
    token = make_verification_token(user)
    path = reverse("verify-email")
    return request.build_absolute_uri(f"{path}?token={token}")


class PasswordStrengthView(APIView):
    """POST /api/auth/password-strength/ — live per-rule check for the signup UI.
    Body: {"password": "..."} → {"rules": {length: bool, uppercase: bool, …}, "valid": bool}.
    Throttled so it can't be abused as an oracle for guessing passwords."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_check"

    def post(self, request):
        password = request.data.get("password", "")
        if not isinstance(password, str):
            return Response({"rules": {}, "valid": False})
        results = password_rule_results(password)
        rules = {rule_id: passed for rule_id, passed in results}
        return Response({"rules": rules, "valid": all(p for _, p in results)})


class RegisterView(generics.CreateAPIView):
    """POST /api/auth/register/ — create an account and email a verification link."""
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        # Best-effort — a mail hiccup must never fail the signup itself.
        send_verification_email(user, _verification_url(request, user))
        return Response(UserProfileSerializer(user).data, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    """GET /api/auth/verify-email/?token=… — mark the account verified (idempotent),
    send the welcome email on first verification, then redirect to the frontend."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def get(self, request):
        token = request.query_params.get("token", "")
        user_id = read_verification_token(token)
        landing = settings.FRONTEND_URL.rstrip("/")

        if not user_id:
            return redirect(f"{landing}/login?verified=invalid")
        try:
            user = User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError):
            return redirect(f"{landing}/login?verified=invalid")

        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = timezone.now()
            user.save(update_fields=["email_verified", "email_verified_at", "updated_at"])
            send_welcome_email(user)  # welcome fires once, on first verification only

        return redirect(f"{landing}/login?verified=1")


class ResendVerificationView(APIView):
    """POST /api/auth/resend-verification/ {email} — re-send the link. Always
    returns 200 with a generic message so it can't be used to probe which
    emails are registered."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        generic = Response(
            {"detail": "If that email needs verification, a new link is on its way."},
            status=status.HTTP_200_OK,
        )
        if not email:
            return generic
        user = User.objects.filter(email__iexact=email, email_verified=False).first()
        if user:
            send_verification_email(user, _verification_url(request, user))
        return generic


class UserProfileView(generics.RetrieveUpdateAPIView):
    """GET/PATCH/PUT /api/auth/me/ — own profile."""
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user



class DeleteAccountRequestView(APIView):
    """POST /api/auth/delete-request/ — Privacy Policy: a guest may ask for their
    data to be deleted. Bookings/payments are financial records we must keep
    for the retention period, so this raises a ticket for the team rather than
    deleting anything on the spot."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        from payments.alerts import alert_admin
        u = request.user
        alert_admin(
            f"delete-request-{u.pk}", f"Account deletion request: {u.email}",
            f"{u.first_name} {u.last_name} <{u.email}> asked for their account and personal data to be deleted "
            f"(user id {u.pk}). Review their bookings' retention obligations, then delete or anonymise.",
        )
        return Response({"detail": "We've received your request and will confirm by email once it has been reviewed."})
