from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
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
    path("admin/", admin.site.urls),

    # Health check (used by Docker HEALTHCHECK and Nginx probes)
    path("api/health/", health_check, name="health-check"),

    # Auth
    path("api/auth/", include("accounts.urls")),

    # Packages (reviews are nested inside packages/urls.py)
    path("api/packages/", include("packages.urls")),

    # Bookings
    path("api/bookings/", include("bookings.urls")),

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
