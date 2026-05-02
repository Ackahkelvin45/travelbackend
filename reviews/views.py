from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticated

from packages.models import TravelPackage
from .models import Review
from .serializers import ReviewSerializer
from .permissions import IsReviewOwnerOrAdmin

_review_post_body = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    required=["reviewer_name", "reviewer_email", "rating", "body"],
    properties={
        "reviewer_name": openapi.Schema(type=openapi.TYPE_STRING, example="Jane Doe"),
        "reviewer_email": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_EMAIL, example="jane@example.com"),
        "rating": openapi.Schema(type=openapi.TYPE_INTEGER, minimum=1, maximum=5, example=5),
        "title": openapi.Schema(type=openapi.TYPE_STRING, example="Amazing safari experience!"),
        "body": openapi.Schema(type=openapi.TYPE_STRING, example="The guide was knowledgeable and the wildlife sightings were incredible."),
    },
)

_review_response = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        "id": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_UUID),
        "reviewer_name": openapi.Schema(type=openapi.TYPE_STRING),
        "rating": openapi.Schema(type=openapi.TYPE_INTEGER),
        "title": openapi.Schema(type=openapi.TYPE_STRING),
        "body": openapi.Schema(type=openapi.TYPE_STRING),
        "created_at": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
    },
)


class PackageReviewListCreateView(generics.ListCreateAPIView):
    serializer_class = ReviewSerializer
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_summary="List reviews for a package",
        operation_description="Returns all published reviews for the given package. No authentication required.",
        responses={200: ReviewSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_summary="Submit a review",
        operation_description=(
            "Anyone can submit a review — no account needed. "
            "Include `booking_reference` to automatically receive a **Verified** badge "
            "if the reference matches a confirmed or completed booking for this package."
        ),
        request_body=_review_post_body,
        responses={
            201: _review_response,
            400: "Validation error (invalid rating, bad booking reference, etc.)",
        },
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def _get_package(self):
        return get_object_or_404(TravelPackage, id=self.kwargs["package_id"])

    def get_queryset(self):
        return (
            Review.objects
            .filter(package_id=self.kwargs["package_id"])
            .select_related("user")
            .order_by("-created_at")
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["package"] = self._get_package()
        return ctx


class PackageReviewDestroyView(generics.DestroyAPIView):
    serializer_class = ReviewSerializer
    permission_classes = [IsAuthenticated, IsReviewOwnerOrAdmin]
    lookup_field = "id"

    @swagger_auto_schema(
        operation_summary="Delete a review",
        operation_description=(
            "Authenticated users can delete their own reviews. "
            "Staff can delete any review. "
            "Guest reviews (no linked account) can only be deleted by staff."
        ),
        responses={204: "Deleted", 403: "Permission denied", 404: "Not found"},
    )
    def delete(self, request, *args, **kwargs):
        return super().delete(request, *args, **kwargs)

    def get_queryset(self):
        return Review.objects.filter(package_id=self.kwargs["package_id"])
