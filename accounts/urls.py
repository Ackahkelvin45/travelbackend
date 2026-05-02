from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import RegisterView, UserProfileView

urlpatterns = [
    # Registration
    path("register/", RegisterView.as_view(), name="register"),

    # Profile — retrieve & update own account
    path("me/", UserProfileView.as_view(), name="user-profile"),

    # JWT
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
