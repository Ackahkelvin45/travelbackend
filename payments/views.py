import json
import logging

from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Booking
from .models import Payment
from .paystack import initialize_transaction, verify_transaction, verify_webhook_signature
from .serializers import InitializePaymentSerializer

logger = logging.getLogger(__name__)

# ── Reusable schema fragments ─────────────────────────────────────────────────

_error_schema = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={"detail": openapi.Schema(type=openapi.TYPE_STRING, example="Error message.")},
)

_init_response = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        "authorization_url": openapi.Schema(type=openapi.TYPE_STRING,
            description="Redirect the user here to pay.", example="https://checkout.paystack.com/xxx"),
        "access_code": openapi.Schema(type=openapi.TYPE_STRING,
            description="Pass to Paystack InlineJS popup.", example="3ni8kdavz62431k"),
        "reference": openapi.Schema(type=openapi.TYPE_STRING,
            description="Store this — use it to call /verify/.", example="AZT-PAY-AZT-AB12CD34"),
        "amount": openapi.Schema(type=openapi.TYPE_STRING, example="1200.00"),
        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
        "booking_reference": openapi.Schema(type=openapi.TYPE_STRING, example="AZT-AB12CD34"),
    },
)

_verify_response = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        "status": openapi.Schema(type=openapi.TYPE_STRING,
            enum=["success", "failed", "abandoned", "pending"], example="success"),
        "booking_reference": openapi.Schema(type=openapi.TYPE_STRING, example="AZT-AB12CD34"),
        "booking_status": openapi.Schema(type=openapi.TYPE_STRING,
            enum=["pending", "confirmed", "cancelled", "completed"], example="confirmed"),
        "amount": openapi.Schema(type=openapi.TYPE_STRING, example="1200.00"),
        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
        "paid_at": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME,
            example="2025-07-02T16:00:00Z"),
    },
)


# ── 1. Initialize ─────────────────────────────────────────────────────────────

class InitializePaymentView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_initialize",
        operation_summary="Initialize a Paystack payment",
        operation_description=(
            "Creates a Paystack transaction for an existing booking and returns a "
            "checkout URL (`authorization_url`) and inline popup code (`access_code`).\n\n"
            "**Redirect flow** — redirect the user to `authorization_url`. Paystack sends "
            "them back to your callback URL with `?reference=AZT-PAY-...` in the query string.\n\n"
            "**Popup flow** — pass `access_code` to `PaystackInline.openIframe({ accessCode })`.\n\n"
            "After payment, call `GET /api/payments/verify/{reference}/` to confirm the booking.\n\n"
            "**Idempotent** — calling this twice for the same booking returns the existing "
            "pending payment instead of creating a duplicate."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["booking_id"],
            properties={
                "booking_id": openapi.Schema(
                    type=openapi.TYPE_STRING, format=openapi.FORMAT_UUID,
                    description="UUID of the Booking (from POST /api/bookings/ response).",
                    example="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                ),
            },
        ),
        responses={
            200: openapi.Response("Transaction initialized.", schema=_init_response),
            400: openapi.Response("Booking already paid.", schema=_error_schema),
            404: openapi.Response("Booking not found.", schema=_error_schema),
            502: openapi.Response("Paystack gateway error.", schema=_error_schema),
        },
    )
    def post(self, request):
        serializer = InitializePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking_id = serializer.validated_data["booking_id"]

        try:
            booking = Booking.objects.select_related("package").get(id=booking_id)
        except Booking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        if hasattr(booking, "payment") and booking.payment.status == Payment.Status.SUCCESS:
            return Response({"detail": "This booking has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        if hasattr(booking, "payment"):
            payment = booking.payment
        else:
            payment = Payment(
                booking=booking,
                paystack_reference=f"AZT-PAY-{booking.reference}",
                amount=booking.total_amount,
                currency=booking.currency,
            )

        amount_subunits = int(booking.total_amount * 100)

        try:
            paystack_data = initialize_transaction(
                email=booking.email,
                amount_kobo=amount_subunits,
                reference=payment.paystack_reference,
                currency=booking.currency,
                metadata={
                    "booking_reference": booking.reference,
                    "customer_name": booking.full_name,
                    "package": booking.package.title,
                },
            )
        except Exception as exc:
            logger.exception("Paystack init failed for booking %s: %s", booking_id, exc)
            return Response({"detail": "Payment gateway error. Please try again later."}, status=status.HTTP_502_BAD_GATEWAY)

        payment.paystack_access_code = paystack_data.get("access_code")
        payment.paystack_authorization_url = paystack_data.get("authorization_url")
        payment.save()

        return Response({
            "authorization_url": payment.paystack_authorization_url,
            "access_code": payment.paystack_access_code,
            "reference": payment.paystack_reference,
            "amount": str(booking.total_amount),
            "currency": booking.currency,
            "booking_reference": booking.reference,
        }, status=status.HTTP_200_OK)


# ── 2. Verify ─────────────────────────────────────────────────────────────────

class VerifyPaymentView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_verify",
        operation_summary="Verify a Paystack payment",
        operation_description=(
            "Calls Paystack to confirm the payment status and, on success, marks the "
            "Booking as `confirmed`.\n\n"
            "**When to call**: on your callback page after Paystack redirects back "
            "(`?reference=AZT-PAY-...`), or after InlineJS fires `onSuccess({ reference })`.\n\n"
            "Check `status === 'success'` before showing a confirmation screen. "
            "Pass `booking_reference` to the review form.\n\n"
            "**Safe to retry** — already-confirmed payments return immediately without "
            "hitting Paystack again."
        ),
        manual_parameters=[
            openapi.Parameter(
                "reference", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True,
                description="Paystack payment reference returned by /initialize/.",
                example="AZT-PAY-AZT-AB12CD34",
            ),
        ],
        responses={
            200: openapi.Response("Verification result.", schema=_verify_response),
            404: openapi.Response("Payment reference not found.", schema=_error_schema),
            502: openapi.Response("Paystack gateway error.", schema=_error_schema),
        },
    )
    def get(self, request, reference):
        try:
            payment = Payment.objects.select_related("booking__package").get(paystack_reference=reference)
        except Payment.DoesNotExist:
            return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)

        if payment.status == Payment.Status.SUCCESS:
            return self._build_response(payment)

        try:
            paystack_data = verify_transaction(reference)
        except Exception as exc:
            logger.exception("Paystack verify failed for ref %s: %s", reference, exc)
            return Response({"detail": "Payment gateway error. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)

        paystack_status = paystack_data.get("status")
        payment.gateway_response = paystack_data

        if paystack_status == "success":
            payment.status = Payment.Status.SUCCESS
            payment.paid_at = timezone.now()
            booking = payment.booking
            booking.status = Booking.Status.CONFIRMED
            booking.save(update_fields=["status", "updated_at"])
        elif paystack_status == "failed":
            payment.status = Payment.Status.FAILED
        elif paystack_status == "abandoned":
            payment.status = Payment.Status.ABANDONED

        payment.save()
        return self._build_response(payment)

    @staticmethod
    def _build_response(payment: Payment) -> Response:
        return Response({
            "status": payment.status,
            "booking_reference": payment.booking.reference,
            "booking_status": payment.booking.status,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "paid_at": payment.paid_at,
        }, status=status.HTTP_200_OK)


# ── 3. Webhook ────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_webhook",
        operation_summary="Paystack webhook (server-to-server only)",
        operation_description=(
            "⚠️ **Do NOT call this from your frontend.** This endpoint is called by "
            "Paystack's servers after a successful charge.\n\n"
            "Register `https://yourdomain.com/api/payments/webhook/` in your "
            "Paystack Dashboard under **Settings → Webhooks**.\n\n"
            "**Security**: the `X-Paystack-Signature` HMAC-SHA512 header is validated "
            "before any data is processed. Invalid signatures return HTTP 400.\n\n"
            "**Always returns HTTP 200** — Paystack retries on any other code."
        ),
        manual_parameters=[
            openapi.Parameter(
                "X-Paystack-Signature", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
                description="HMAC-SHA512 of request body signed with your PAYSTACK_SECRET_KEY. Added automatically by Paystack.",
            ),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            description="Paystack event envelope. Only `charge.success` is processed.",
            properties={
                "event": openapi.Schema(type=openapi.TYPE_STRING, example="charge.success"),
                "data": openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "reference": openapi.Schema(type=openapi.TYPE_STRING, example="AZT-PAY-AZT-AB12CD34"),
                        "status": openapi.Schema(type=openapi.TYPE_STRING, example="success"),
                        "amount": openapi.Schema(type=openapi.TYPE_INTEGER, example=120000),
                        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
                    },
                ),
            },
        ),
        responses={
            200: openapi.Response("Acknowledged."),
            400: openapi.Response("Invalid HMAC signature.", schema=_error_schema),
        },
    )
    def post(self, request):
        signature = request.headers.get("X-Paystack-Signature", "")
        if not verify_webhook_signature(request.body, signature):
            logger.warning("Webhook received with invalid Paystack signature.")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = json.loads(request.body)
        except json.JSONDecodeError:
            return Response(status=status.HTTP_200_OK)

        event_type = event.get("event")
        data = event.get("data", {})
        logger.info("Paystack webhook: event=%s", event_type)

        if event_type == "charge.success":
            reference = data.get("reference")
            if not reference:
                return Response(status=status.HTTP_200_OK)

            try:
                payment = Payment.objects.select_related("booking").get(paystack_reference=reference)
            except Payment.DoesNotExist:
                logger.info("Webhook: ref %s not found, skipping.", reference)
                return Response(status=status.HTTP_200_OK)

            if payment.status != Payment.Status.SUCCESS:
                payment.status = Payment.Status.SUCCESS
                payment.gateway_response = data
                payment.paid_at = timezone.now()
                payment.save()
                booking = payment.booking
                booking.status = Booking.Status.CONFIRMED
                booking.save(update_fields=["status", "updated_at"])
                logger.info("Webhook confirmed ref=%s booking=%s", reference, booking.reference)

        return Response(status=status.HTTP_200_OK)
