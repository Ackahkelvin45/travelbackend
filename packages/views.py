from django.db.models import Avg, Count, Q
from django.utils import timezone

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, permissions, filters
from rest_framework.response import Response
from rest_framework.views import APIView

from .filters import TravelPackageFilter
from .models import TravelPackage, Itinerary, PackageImage, PackageFAQ
from .serializers import (
    TravelPackageListSerializer,
    TravelPackageDetailSerializer,
    ItinerarySerializer,
    PackageImageSerializer,
    PackageFAQSerializer,
)


class IsAdminOrReadOnly(permissions.BasePermission):
    """Allow anyone to read; only admins can write."""
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user and request.user.is_staff


# ──────────────────────────────────────────
# TRAVEL PACKAGE
# ──────────────────────────────────────────

def _base_package_qs():
    return TravelPackage.objects.annotate(
        avg_rating=Avg("reviews__rating"),
        review_count=Count("reviews", distinct=True),
    )


class TravelPackageListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/packages/    — upcoming / active packages (past events excluded)
    POST /api/packages/    — create a package (admin only)

    Filters:
        travel_from   dd/mm/yyyy   Travel window start
        travel_to     dd/mm/yyyy   Travel window end
        category      string       luxury_travel | nightlife | cultural | culinary | fashion | corporate | bespoke
        price_min     number       Minimum shared price
        price_max     number       Maximum shared price
        duration      string       1d | 2-3d | 4-7d | 1-2w | 2w+
    """
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TravelPackageFilter
    search_fields = ["title", "destination", "description"]
    ordering_fields = ["price_shared", "duration_days", "created_at"]

    def get_queryset(self):
        today = timezone.now().date()
        qs = _base_package_qs().filter(
            # Exclude packages whose availability window has fully passed.
            Q(available_to__isnull=True) | Q(available_to__gte=today)
        )
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(is_active=True)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TravelPackageDetailSerializer
        return TravelPackageListSerializer


class PastPackagesView(generics.ListAPIView):
    """
    GET /api/packages/past/   — packages whose available_to is in the past.
    Useful for showcasing completed events / trip history.
    """
    serializer_class = TravelPackageListSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "destination", "description"]
    ordering_fields = ["available_to", "price_shared", "created_at"]

    def get_queryset(self):
        today = timezone.now().date()
        qs = _base_package_qs().filter(available_to__lt=today)
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(is_active=True)
        return qs


class TrendingPackagesView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        qs = TravelPackage.objects.filter(is_active=True).order_by("-created_at")[:10]
        serializer = TravelPackageListSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)


class TravelPackageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/packages/{id}/  — retrieve a package with full itinerary
    PATCH  /api/packages/{id}/  — partial update (admin only)
    PUT    /api/packages/{id}/  — full update (admin only)
    DELETE /api/packages/{id}/  — delete (admin only)
    """
    queryset = TravelPackage.objects.annotate(
        avg_rating=Avg("reviews__rating"),
        review_count=Count("reviews", distinct=True),
    )
    serializer_class = TravelPackageDetailSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"


# ──────────────────────────────────────────
# ITINERARY  (nested under a package)
# ──────────────────────────────────────────

class ItineraryListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/packages/{package_id}/itineraries/   — list all days
    POST /api/packages/{package_id}/itineraries/   — add a day (admin only)
    """
    serializer_class = ItinerarySerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        return Itinerary.objects.filter(package_id=self.kwargs["package_id"])

    def perform_create(self, serializer):
        package = generics.get_object_or_404(TravelPackage, id=self.kwargs["package_id"])
        serializer.save(package=package)


class ItineraryDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/packages/{package_id}/itineraries/{id}/  — retrieve a day
    PATCH  /api/packages/{package_id}/itineraries/{id}/  — update (admin only)
    DELETE /api/packages/{package_id}/itineraries/{id}/  — delete (admin only)
    """
    serializer_class = ItinerarySerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"

    def get_queryset(self):
        return Itinerary.objects.filter(package_id=self.kwargs["package_id"])


# ──────────────────────────────────────────
# PACKAGE IMAGES  (nested under a package)
# ──────────────────────────────────────────

class PackageImageListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/packages/{package_id}/images/   — list all images
    POST /api/packages/{package_id}/images/   — upload an image (admin only)
    """
    serializer_class = PackageImageSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        return PackageImage.objects.filter(package_id=self.kwargs["package_id"])

    def perform_create(self, serializer):
        package = generics.get_object_or_404(TravelPackage, id=self.kwargs["package_id"])
        serializer.save(package=package)


class PackageImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/packages/{package_id}/images/{id}/  — retrieve an image
    PATCH  /api/packages/{package_id}/images/{id}/  — update caption/order/cover (admin only)
    DELETE /api/packages/{package_id}/images/{id}/  — delete (admin only)
    """
    serializer_class = PackageImageSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"

    def get_queryset(self):
        return PackageImage.objects.filter(package_id=self.kwargs["package_id"])


# ──────────────────────────────────────────
# PACKAGE FAQs  (nested under a package)
# ──────────────────────────────────────────

class PackageFAQListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/packages/{package_id}/faqs/   — list all FAQs
    POST /api/packages/{package_id}/faqs/   — add an FAQ (admin only)
    """
    serializer_class = PackageFAQSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        return PackageFAQ.objects.filter(package_id=self.kwargs["package_id"])

    def perform_create(self, serializer):
        package = generics.get_object_or_404(TravelPackage, id=self.kwargs["package_id"])
        serializer.save(package=package)


class PackageFAQDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/packages/{package_id}/faqs/{id}/  — retrieve an FAQ
    PATCH  /api/packages/{package_id}/faqs/{id}/  — update (admin only)
    DELETE /api/packages/{package_id}/faqs/{id}/  — delete (admin only)
    """
    serializer_class = PackageFAQSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"

    def get_queryset(self):
        return PackageFAQ.objects.filter(package_id=self.kwargs["package_id"])
