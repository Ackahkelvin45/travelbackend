import logging

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Booking, Lead
from .pricing import QuoteError, compute_component_quote, compute_configurable_quote, compute_quote
from .serializers import (
    BookingDetailSerializer,
    CheckoutSerializer,
    ComponentCheckoutSerializer,
    ComponentQuoteSerializer,
    ConfigurableCheckoutSerializer,
    CreateBookingSerializer,
    HoldSerializer,
    LeadSerializer,
    TravelInfoSerializer,
)
from .services import (
    PolicyAcceptanceRequired,
    create_configurable_booking,
    create_option_booking,
)

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


class CheckoutView(APIView):
    """Option-based booking creation (hotel × occupancy flagship flow)."""

    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_checkout",
        operation_summary="Create an option-based booking (flagship flow)",
        operation_description=(
            "Creates a fully-snapshotted booking for a hotel/occupancy option. "
            "All prices are computed server-side from the package's pricing matrix; "
            "early-bird eligibility locks at creation time.\n\n"
            "Every currently-published policy document must be listed in "
            "`accepted_policies` — acceptances are recorded transactionally with "
            "the booking.\n\n"
            "**409** is returned when `expected_total` no longer matches the "
            "server's price (e.g. early bird expired while the page was open); "
            "the response includes a fresh quote to re-confirm with the customer."
        ),
        request_body=CheckoutSerializer,
        responses={
            201: openapi.Response("Booking created.", schema=_booking_create_response),
            400: openapi.Response("Validation / policy acceptance error.", schema=_error_schema),
            404: openapi.Response("Option not found.", schema=_error_schema),
            409: openapi.Response("Price changed — re-confirm with the fresh quote."),
        },
    )
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from packages.models import PackageOption

        try:
            option = PackageOption.objects.select_related("package").get(id=data["option_id"])
        except PackageOption.DoesNotExist:
            return Response({"detail": "Room option not found."}, status=status.HTTP_404_NOT_FOUND)

        # Stale-page guard: the customer confirmed a number — if it changed
        # (early bird expired, admin repriced), never silently charge the new
        # one. 409 + fresh quote → the UI re-confirms explicitly.
        try:
            quote = compute_quote(option, visa=data["visa"], payment_plan=data["payment_plan"])
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        expected_total = data.get("expected_total")
        if expected_total is not None and expected_total != quote.total:
            return Response(
                {
                    "detail": "The price has changed since this page was loaded.",
                    "quote": quote.as_dict(),
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            booking = create_option_booking(
                option=option,
                visa=data["visa"],
                payment_plan=data["payment_plan"],
                contact={
                    "first_name": data["first_name"],
                    "last_name": data["last_name"],
                    "email": data["email"],
                    "phone": data.get("phone") or None,
                    "country": data.get("country") or None,
                    "special_requests": data.get("special_requests") or None,
                },
                accepted_policy_types=data["accepted_policies"],
                user=request.user if request.user.is_authenticated else None,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PolicyAcceptanceRequired as exc:
            return Response(
                {
                    "detail": "All booking policies must be accepted.",
                    "missing_policies": exc.missing_types,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "id": str(booking.id),
            "reference": booking.reference,
            "total_amount": str(booking.total_amount),
            "amount_due_today": str(
                booking.deposit_required
                if booking.payment_plan == Booking.PaymentPlan.INSTALLMENT
                else booking.total_amount
            ),
            "deposit_required": str(booking.deposit_required) if booking.deposit_required else None,
            "early_bird_applied": booking.early_bird_applied,
            "currency": booking.currency,
            "status": booking.status,
            "message": "Booking created. Proceed to payment.",
        }, status=status.HTTP_201_CREATED)


class ConfigurableCheckoutView(APIView):
    """POST /api/bookings/checkout/configurable/ — core_plus_addons flow:
    mandatory base + chosen add-ons + the highest qualifying bundle discount.
    Prices are server-computed and snapshotted; a stale cart gets a 409 + fresh
    quote (same guard as the option flow)."""

    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_checkout_configurable",
        operation_summary="Create a core + add-ons booking",
        request_body=ConfigurableCheckoutSerializer,
        responses={
            201: openapi.Response("Booking created."),
            400: openapi.Response("Validation / policy / add-on error.", schema=_error_schema),
            404: openapi.Response("Package not found.", schema=_error_schema),
            409: openapi.Response("Price changed — re-confirm with the fresh quote."),
        },
    )
    def post(self, request):
        serializer = ConfigurableCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from packages.models import TravelPackage

        try:
            package = TravelPackage.objects.get(id=data["package_id"])
        except TravelPackage.DoesNotExist:
            return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            quote = compute_configurable_quote(
                package, num_guests=data["num_guests"],
                selected_addon_codes=data["selected_addon_codes"],
                payment_plan=data["payment_plan"],
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        expected_total = data.get("expected_total")
        if expected_total is not None and expected_total != quote.total:
            return Response(
                {"detail": "The price has changed since this page was loaded.", "quote": quote.as_dict()},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            booking = create_configurable_booking(
                package=package,
                num_guests=data["num_guests"],
                selected_addon_codes=data["selected_addon_codes"],
                payment_plan=data["payment_plan"],
                contact={
                    "first_name": data["first_name"],
                    "last_name": data["last_name"],
                    "email": data["email"],
                    "phone": data.get("phone") or None,
                    "country": data.get("country") or None,
                    "special_requests": data.get("special_requests") or None,
                },
                accepted_policy_types=data["accepted_policies"],
                user=request.user if request.user.is_authenticated else None,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PolicyAcceptanceRequired as exc:
            return Response(
                {"detail": "All booking policies must be accepted.", "missing_policies": exc.missing_types},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "id": str(booking.id),
            "reference": booking.reference,
            "total_amount": str(booking.total_amount),
            "discount_amount": str(booking.discount_amount),
            "amount_due_today": str(
                booking.deposit_required
                if booking.payment_plan == Booking.PaymentPlan.INSTALLMENT
                else booking.total_amount
            ),
            "deposit_required": str(booking.deposit_required) if booking.deposit_required else None,
            "currency": booking.currency,
            "status": booking.status,
            "message": "Booking created. Proceed to payment.",
        }, status=status.HTTP_201_CREATED)


class CurrentPoliciesView(APIView):
    """GET /api/bookings/policies/ — the currently-published policy documents
    customers must accept at checkout."""

    permission_classes = [AllowAny]

    def get(self, request):
        from .models import PolicyDocument

        docs = PolicyDocument.objects.filter(is_current=True, published_at__isnull=False)
        return Response([
            {
                "type": d.type,
                "type_display": d.get_type_display(),
                "version": d.version,
                "title": d.title,
                "body": d.body,
                "is_required": d.is_required,
                "published_at": d.published_at,
            }
            for d in docs
        ])


class MyBookingsView(APIView):
    """Authenticated dashboard listing — strictly the user's own bookings."""

    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_mine",
        operation_summary="List my bookings (dashboard)",
        operation_description=(
            "Full booking detail (totals, balance, payment ledger, deadlines) for "
            "every booking attached to the authenticated account. Guest bookings "
            "are attached via the claim link in the confirmation email."
        ),
        responses={200: BookingDetailSerializer(many=True)},
    )
    def get(self, request):
        bookings = (
            Booking.objects
            .filter(user=request.user)
            .select_related("package")
            .prefetch_related("payments")
            .order_by("-created_at")
        )
        # Hide stale abandoned checkouts from the dashboard.
        visible = [b for b in bookings if not b.is_expired]
        return Response(BookingDetailSerializer(visible, many=True).data, status=status.HTTP_200_OK)


class ClaimBookingView(APIView):
    """
    Attach a guest booking to the calling account via the signed token from
    the confirmation email. Email-match alone never grants access — anyone can
    register any address, so possession of the emailed link is the proof.
    """

    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=["Bookings"],
        operation_id="booking_claim",
        operation_summary="Claim a guest booking",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["token"],
            properties={"token": openapi.Schema(type=openapi.TYPE_STRING)},
        ),
        responses={
            200: openapi.Response("Booking attached to this account."),
            400: openapi.Response("Invalid or expired token.", schema=_error_schema),
        },
    )
    def post(self, request):
        from django.core import signing

        token = request.data.get("token", "")
        try:
            payload = signing.loads(token, salt="booking-claim", max_age=60 * 60 * 24 * 90)
        except signing.BadSignature:
            return Response({"detail": "Invalid or expired claim link."}, status=status.HTTP_400_BAD_REQUEST)

        booking = Booking.objects.filter(id=payload.get("booking")).first()
        if booking is None:
            return Response({"detail": "Booking not found."}, status=status.HTTP_400_BAD_REQUEST)
        if booking.user and booking.user != request.user:
            return Response({"detail": "This booking belongs to another account."}, status=status.HTTP_400_BAD_REQUEST)

        booking.user = request.user
        booking.save(update_fields=["user", "updated_at"])
        return Response({"reference": booking.reference, "detail": "Booking attached to your account."})


class _OwnedBooking:
    """Owner-only lookup: a signed-in user may act only on bookings attached to
    their account (guests attach via the emailed claim link)."""

    @staticmethod
    def get(request, reference):
        return (
            Booking.objects.select_related("package")
            .filter(reference=reference, user=request.user)
            .first()
        )


class CancellationQuoteView(APIView):
    """GET /api/bookings/<reference>/cancellation-quote/ — what the guest would
    be refunded if they requested cancellation right now (Refund Policy bands
    applied to money actually paid). Preview only; nothing is recorded."""

    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        from payments.refunds import compute_refund

        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        eligible = booking.status in (Booking.Status.PENDING, Booking.Status.CONFIRMED)
        computed = compute_refund(booking)
        return Response({
            "eligible": eligible and not booking.pending_cancellation,
            "pending": bool(booking.pending_cancellation),
            "refund_total": str(computed["refund_total"]),
            "currency": booking.currency,
            "breakdown": computed["breakdown"],
            "arrival_rule": "No refund once you have arrived in Ghana or the experience has started.",
        })


class AvailableAddonsView(APIView):
    """GET /api/bookings/<reference>/addons/available/ — experiences the owner
    can still add (until 7 days before departure), priced at face value. Paying
    for one (POST /payments/initialize/ intent=addon) puts it on the booking."""

    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        from .services import addon_additions_open, available_addons

        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        ok, reason, open_until = addon_additions_open(booking)
        return Response({
            "can_add": ok, "reason": reason, "open_until": open_until,
            "currency": booking.currency,
            "addons": available_addons(booking) if ok else [],
        })


class CancellationRequestView(APIView):
    """POST /api/bookings/<reference>/cancellation-request/ {reason?} — record
    the request as of now, quote the refund at this instant, acknowledge by
    email, and freeze further payments until staff resolve it."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request, reference):
        from .services import IllegalTransition, request_cancellation

        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        reason = str(request.data.get("reason", ""))[:2000]
        try:
            req = request_cancellation(booking, reason=reason)
        except IllegalTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": "Your cancellation request has been received.",
            "requested_at": req.requested_at,
            "refund_total": req.refund_quote.get("refund_total"),
            "currency": booking.currency,
        }, status=status.HTTP_201_CREATED)


class BookingStatusView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "booking-status"

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
        # Try finding by booking reference first
        booking = (
            Booking.objects
            .select_related("package")
            .prefetch_related("payments")
            .filter(reference=reference)
            .first()
        )

        # If not found, try finding by the associated payment reference (e.g. AZT-PAY-AZT-...)
        if not booking:
            booking = (
                Booking.objects
                .select_related("package")
                .prefetch_related("payments")
                .filter(payments__paystack_reference=reference)
                .first()
            )

        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = BookingDetailSerializer(booking)
        return Response(serializer.data, status=status.HTTP_200_OK)


# ── Component pricing (Change Request v2) ─────────────────────────────────────

def _component_package(package_id):
    from packages.models import TravelPackage
    return TravelPackage.objects.filter(id=package_id, pricing_model=TravelPackage.PricingModel.COMPONENT).first()


class ComponentQuoteView(APIView):
    """POST /api/bookings/quote/ — the server total for a trip-builder
    selection. The UI shows this; it never adds prices up itself."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quote"

    def post(self, request):
        serializer = ComponentQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        package = _component_package(data["package_id"])
        if package is None:
            return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            quote = compute_component_quote(
                package, mode=data["mode"], travellers=data["travellers"],
                rooms=data["rooms"], payment_plan=data["payment_plan"],
                hold_token=data.get("hold_token", ""),
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(quote.as_dict())


class HoldView(APIView):
    """POST /api/bookings/holds/ — hold rooms while the guest checks out.
    DELETE /api/bookings/holds/<token>/ — give them back."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "holds"

    def post(self, request):
        from django.utils import timezone

        from .services import create_hold

        serializer = HoldSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        package = _component_package(data["package_id"])
        if package is None:
            return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            token, held_until, units = create_hold(
                package, data["stay_types"], replace_token=data.get("replace_token", ""),
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({
            "token": token, "held_until": held_until, "server_now": timezone.now(),
            "units": [{"stay_type": u.stay_type.code, "label": u.label} for u in units],
        }, status=status.HTTP_201_CREATED)

    def delete(self, request, token):
        from .services import release_hold
        release_hold(token)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ComponentCheckoutView(APIView):
    """POST /api/bookings/checkout/component/ — hold → booking with one Core
    ticket per traveller. Then POST /api/payments/initialize/ as usual."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "holds"

    def post(self, request):
        from .services import create_component_booking

        serializer = ComponentCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        package = _component_package(data["package_id"])
        if package is None:
            return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            quote = compute_component_quote(
                package, mode=data["mode"], travellers=data["travellers"], rooms=data["rooms"],
                payment_plan=data["payment_plan"], check_availability=False,
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        expected_total = data.get("expected_total")
        if expected_total is not None and expected_total != quote.total:
            return Response(
                {"detail": "The price has changed since this page was loaded.", "quote": quote.as_dict()},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            booking = create_component_booking(
                package=package, mode=data["mode"], travellers=data["travellers"], rooms=data["rooms"],
                payment_plan=data["payment_plan"], hold_token=data.get("hold_token", ""),
                contact={
                    "first_name": data["first_name"], "last_name": data["last_name"], "email": data["email"],
                    "phone": data.get("phone") or None, "country": data.get("country") or None,
                    "special_requests": data.get("special_requests") or None,
                },
                accepted_policy_types=data["accepted_policies"],
                marketing_consent=data["marketing_consent"],
                user=request.user if request.user.is_authenticated else None,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except QuoteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PolicyAcceptanceRequired as exc:
            return Response(
                {"detail": "All booking policies must be accepted.", "missing_policies": exc.missing_types},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            "id": str(booking.id),
            "reference": booking.reference,
            "total_amount": str(booking.total_amount),
            "discount_amount": str(booking.discount_amount),
            "amount_due_today": booking.quote_snapshot["amount_due_today"],
            "deposit_required": str(booking.deposit_required) if booking.deposit_required else None,
            "currency": booking.currency,
            "status": booking.status,
            "quote": booking.quote_snapshot,
            "message": "Booking created. Proceed to payment.",
        }, status=status.HTTP_201_CREATED)


class TravelInfoView(APIView):
    """GET/PUT /api/bookings/<reference>/travel/ — owner-only post-booking
    form. PUT {"travellers": [{"id": ..., "arrival_flight": ..., ...}]}."""

    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"travellers": TravelInfoSerializer(booking.travellers.all(), many=True).data})

    def put(self, request, reference):
        from django.db import transaction
        from django.utils import timezone

        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        rows = request.data.get("travellers")
        if not isinstance(rows, list):
            return Response({"detail": "Send a list of travellers."}, status=status.HTTP_400_BAD_REQUEST)
        travellers = {str(t.id): t for t in booking.travellers.all()}
        with transaction.atomic():
            for row in rows:
                traveller = travellers.get(str(row.get("id")) if isinstance(row, dict) else None)
                if traveller is None:
                    return Response({"detail": "Unknown traveller."}, status=status.HTTP_400_BAD_REQUEST)
                serializer = TravelInfoSerializer(traveller, data=row, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save(travel_info_submitted_at=timezone.now())
        return Response({"travellers": TravelInfoSerializer(booking.travellers.all(), many=True).data})


class BookingPreferencesView(APIView):
    """PATCH /api/bookings/<reference>/preferences/ {marketing_consent?, photo_consent?}
    — owner-only; consent can be withdrawn or given at any time."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, reference):
        booking = _OwnedBooking.get(request, reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        fields = [f for f in ("marketing_consent", "photo_consent") if isinstance(request.data.get(f), bool)]
        for f in fields:
            setattr(booking, f, request.data[f])
        if fields:
            booking.save(update_fields=[*fields, "updated_at"])
        return Response({"marketing_consent": booking.marketing_consent, "photo_consent": booking.photo_consent})


class LeadCreateView(APIView):
    """POST /api/leads/ — chat hand-off, contact form or saved quote."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "leads"

    def post(self, request):
        from datetime import timedelta

        from django.utils import timezone

        from payments.models import OpsConfig

        serializer = LeadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        selection = data.pop("selection", None)
        quote = {}
        if selection:
            package = _component_package(selection["package_id"])
            if package is None:
                return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)
            try:
                quote = compute_component_quote(
                    package, mode=selection["mode"], travellers=selection["travellers"],
                    rooms=selection["rooms"], payment_plan=selection["payment_plan"], check_availability=False,
                ).as_dict()
            except QuoteError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            data["package"] = package
        lead = Lead.objects.create(
            **data, quote=quote,
            reply_due_at=timezone.now() + timedelta(hours=OpsConfig.get().lead_reply_hours),
        )
        return Response({"id": str(lead.id), "detail": "Thanks — our team will be in touch."},
                        status=status.HTTP_201_CREATED)


class SiteSettingsView(APIView):
    """GET /api/site-settings/ — public contact details and hours. Blank
    values mean "not confirmed yet": the site keeps its own fallback."""

    permission_classes = [AllowAny]

    def get(self, request):
        from payments.models import OpsConfig

        c = OpsConfig.get()
        return Response({
            "contact_email": c.contact_email, "contact_phone": c.contact_phone,
            "whatsapp_number": c.whatsapp_number, "emergency_phone": c.emergency_phone,
            "staffed_hours": c.staffed_hours, "legal_name": c.legal_name,
            "legal_address": c.legal_address, "hold_minutes": c.hold_minutes,
        })
