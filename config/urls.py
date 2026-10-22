from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from bookings.reports import reports_view
from bookings import whatsapp
from bookings.views import AssistantView, LeadCreateView, LeadUnsubscribeView, SiteSettingsView
from payments.donations import DonationCreateView, DonationVerifyView
from django.http import JsonResponse
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi


def health_check(request):
    """Lightweight health-check endpoint for Docker/Nginx probes."""
    return JsonResponse({"status": "ok"}, status=200)

schema_view = get_schema_view(
    openapi.Info(
        title="Travel & Tour API",
        default_version="v1",
        description="API for managing travel packages, bookings, payments, and reviews.",
        contact=openapi.Contact(email="admin@travelandtour.com"),
        license=openapi.License(name="Proprietary"),
    ),
    public=True,
    permission_classes=[permissions.AllowAny],
)

urlpatterns = [
    path("admin/reports/", admin.site.admin_view(reports_view), name="admin-reports"),
    # Staff password reset from the admin sign-in screen
    path("admin/password_reset/", auth_views.PasswordResetView.as_view(), name="admin_password_reset"),
    path("admin/password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("admin/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("admin/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("admin/", admin.site.urls),

    # Health check (used by Docker HEALTHCHECK and Nginx probes)
    path("api/health/", health_check, name="health-check"),

    # Auth
    path("api/auth/", include("accounts.urls")),

    # Packages (reviews are nested inside packages/urls.py)
    path("api/packages/", include("packages.urls")),

    # Bookings
    path("api/bookings/", include("bookings.urls")),
    path("api/leads/", LeadCreateView.as_view(), name="lead-create"),
    path("api/leads/unsubscribe/", LeadUnsubscribeView.as_view(), name="lead-unsubscribe"),
    path("api/site-settings/", SiteSettingsView.as_view(), name="site-settings"),
    path("api/donations/", DonationCreateView.as_view(), name="donation-create"),
    path("api/donations/verify/<str:reference>/", DonationVerifyView.as_view(), name="donation-verify"),
    path("api/assistant/", AssistantView.as_view(), name="assistant"),
    path("api/whatsapp/webhook/", whatsapp.webhook, name="whatsapp-webhook"),

    # Payments (initialize, verify, webhook)
    path("api/payments/", include("payments.urls")),

    # Blog
    path("api/blog/", include("blog.urls")),

    # Newsletter
    path("api/newsletter/", include("newsletter.urls")),

    # Swagger / ReDoc
    path("api/docs/", schema_view.with_ui("swagger", cache_timeout=0), name="schema-swagger-ui"),
    path("api/redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),
]

# Serve media files (Nginx handles this in production, Django dev server handles it locally)
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
