from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import (
    RegisterView,
    ResendVerificationView,
    UserProfileView,
    VerifyEmailView,
)

urlpatterns = [
    # Registration
    path("register/", RegisterView.as_view(), name="register"),

    # Email verification
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend-verification"),

    # Profile — retrieve & update own account
    path("me/", UserProfileView.as_view(), name="user-profile"),

    # JWT
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
