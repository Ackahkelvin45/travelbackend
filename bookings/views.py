import logging

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Booking
from .serializers import BookingDetailSerializer, CreateBookingSerializer

logger = logging.getLogger(__name__)

_error_schema = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={"detail": openapi.Schema(type=openapi.TYPE_STRING, example="Error message.")},
)

_booking_create_response = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        "id": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_UUID,
            description="Booking UUID — pass this to POST /api/payments/initialize/.",
            example="a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
        "reference": openapi.Schema(type=openapi.TYPE_STRING,
            description="Human-readable booking reference.", example="AZT-AB12CD34"),
        "total_amount": openapi.Schema(type=openapi.TYPE_STRING, example="1200.00"),
        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
        "status": openapi.Schema(type=openapi.TYPE_STRING, example="pending"),
        "message": openapi.Schema(type=openapi.TYPE_STRING,
            example="Booking created. Proceed to payment."),
    },
)

_booking_detail_response = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        "id": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_UUID),
        "reference": openapi.Schema(type=openapi.TYPE_STRING, example="AZT-AB12CD34"),
        "first_name": openapi.Schema(type=openapi.TYPE_STRING, example="Kwame"),
        "last_name": openapi.Schema(type=openapi.TYPE_STRING, example="Mensah"),
        "email": openapi.Schema(type=openapi.TYPE_STRING, example="kwame@example.com"),
        "phone": openapi.Schema(type=openapi.TYPE_STRING, example="+233201234567"),
        "country": openapi.Schema(type=openapi.TYPE_STRING, example="Ghana"),
        "package_title": openapi.Schema(type=openapi.TYPE_STRING,
            example="Accra City & Culture Tour"),
        "num_guests": openapi.Schema(type=openapi.TYPE_INTEGER, example=2),
        "travel_date": openapi.Schema(type=openapi.TYPE_STRING,
            format=openapi.FORMAT_DATE, example="2025-07-15"),
        "unit_price": openapi.Schema(type=openapi.TYPE_STRING, example="600.00"),
        "total_amount": openapi.Schema(type=openapi.TYPE_STRING, example="1200.00"),
        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
        "status": openapi.Schema(type=openapi.TYPE_STRING,
            enum=["pending", "confirmed", "cancelled", "completed"], example="confirmed"),
        "payment_status": openapi.Schema(type=openapi.TYPE_STRING,
            enum=["pending", "success", "failed", "abandoned", "refunded"],
            description="Null if payment not yet initialized.", example="success"),
        "payment_reference": openapi.Schema(type=openapi.TYPE_STRING,
            description="Paystack reference — pass to /api/payments/verify/.",
            example="AZT-PAY-AZT-AB12CD34"),
        "created_at": openapi.Schema(type=openapi.TYPE_STRING,
            format=openapi.FORMAT_DATETIME, example="2025-07-01T12:00:00Z"),
    },
)


class CreateBookingView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_create",
        operation_summary="Create a booking",
        operation_description=(
            "Creates a Booking in `pending` status. Works for both guests (no auth token "
            "required) and registered users (include a JWT Bearer token to auto-link the "
            "booking to the user's account).\n\n"
            "**Price is resolved server-side** — send `price_tier` and the backend reads "
            "the correct price from the package. The frontend cannot manipulate the amount.\n\n"
            "**Next step**: take the returned `id` (UUID) and call "
            "`POST /api/payments/initialize/` to get the Paystack checkout URL."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["package_id", "first_name", "last_name", "email"],
            properties={
                "package_id": openapi.Schema(type=openapi.TYPE_STRING,
                    format=openapi.FORMAT_UUID,
                    description="UUID of the TravelPackage to book.",
                    example="d9f1e2b3-4c5a-6789-abcd-ef1234567890"),
                "price_tier": openapi.Schema(type=openapi.TYPE_STRING,
                    enum=["shared", "private", "vip"], default="shared",
                    description="Which pricing option. Resolved to the package price server-side."),
                "first_name": openapi.Schema(type=openapi.TYPE_STRING, example="Kwame"),
                "last_name": openapi.Schema(type=openapi.TYPE_STRING, example="Mensah"),
                "email": openapi.Schema(type=openapi.TYPE_STRING,
                    format=openapi.FORMAT_EMAIL, example="kwame@example.com"),
                "phone": openapi.Schema(type=openapi.TYPE_STRING, example="+233201234567"),
                "country": openapi.Schema(type=openapi.TYPE_STRING, example="Ghana"),
                "num_guests": openapi.Schema(type=openapi.TYPE_INTEGER, default=1,
                    description="Number of guests. Total = unit_price × num_guests.", example=2),
                "travel_date": openapi.Schema(type=openapi.TYPE_STRING,
                    format=openapi.FORMAT_DATE, example="2025-07-15"),
                "special_requests": openapi.Schema(type=openapi.TYPE_STRING,
                    description="Optional notes for the tour operator.",
                    example="Vegetarian meals please"),
            },
        ),
        responses={
            201: openapi.Response("Booking created.", schema=_booking_create_response),
            400: openapi.Response("Validation error (missing fields, invalid package, unavailable tier).", schema=_error_schema),
        },
    )
    def post(self, request):
        serializer = CreateBookingSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()

        logger.info("Booking created: ref=%s email=%s total=%s %s",
            booking.reference, booking.email, booking.total_amount, booking.currency)

        return Response({
            "id": str(booking.id),
            "reference": booking.reference,
            "total_amount": str(booking.total_amount),
            "currency": booking.currency,
            "status": booking.status,
            "message": "Booking created. Proceed to payment.",
        }, status=status.HTTP_201_CREATED)


class BookingStatusView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_status",
        operation_summary="Get booking status",
        operation_description=(
            "Returns full booking detail including payment status. "
            "Poll this after payment to confirm the booking is `confirmed`.\n\n"
            "Also use the returned `payment_reference` and `reference` to:\n"
            "- Re-open the verify endpoint if the user navigates back\n"
            "- Pre-fill the booking reference on the review/leave-reply form\n\n"
            "**No auth required** — accessible by anyone with the booking reference."
        ),
        manual_parameters=[
            openapi.Parameter(
                "reference", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True,
                description="Booking reference in format AZT-XXXXXXXX.",
                example="AZT-AB12CD34",
            ),
        ],
        responses={
            200: openapi.Response("Booking detail.", schema=_booking_detail_response),
            404: openapi.Response("Booking not found.", schema=_error_schema),
        },
    )
    def get(self, request, reference):
        try:
            booking = (
                Booking.objects
                .select_related("package")
                .prefetch_related("payment")
                .get(reference=reference)
            )
        except Booking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = BookingDetailSerializer(booking)
        return Response(serializer.data, status=status.HTTP_200_OK)
