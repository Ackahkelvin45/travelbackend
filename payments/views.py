import json
import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from bookings.models import Booking
from .models import Payment
from .money import quantize, to_subunits
from .paystack import (
    DIRECT_MOMO_PROVIDERS,
    charge_mobile_money,
    initialize_transaction,
    verify_transaction,
    verify_webhook_signature,
)
from .serializers import InitializePaymentSerializer
from .services import (
    apply_successful_payment,
    generate_payment_reference,
    mark_payment_unsuccessful,
)

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
            description="Store this — use it to call /verify/.", example="AZT-PAY-AZT-AB12CD34-9F3C21"),
        "amount": openapi.Schema(type=openapi.TYPE_STRING, example="1200.00"),
        "currency": openapi.Schema(type=openapi.TYPE_STRING, example="GHS"),
        "booking_reference": openapi.Schema(type=openapi.TYPE_STRING, example="AZT-AB12CD34"),
    },
)

_status_response = openapi.Schema(
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
        "amount_paid": openapi.Schema(type=openapi.TYPE_STRING, example="1000.00"),
        "balance": openapi.Schema(type=openapi.TYPE_STRING, example="200.00"),
    },
)


def _payment_status_payload(payment: Payment) -> dict:
    booking = payment.booking
    return {
        "status": payment.status,
        "booking_reference": booking.reference,
        "booking_status": booking.status,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "paid_at": payment.paid_at,
        "amount_paid": str(booking.amount_paid),
        "balance": str(booking.balance),
    }


# ── 1. Initialize ─────────────────────────────────────────────────────────────

class InitializePaymentView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payments"

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_initialize",
        operation_summary="Initialize a Paystack payment",
        operation_description=(
            "Creates a fresh Paystack transaction for an existing booking and returns a "
            "checkout URL (`authorization_url`) and inline popup code (`access_code`).\n\n"
            "Each call mints a **new payment attempt with a fresh reference** — retrying "
            "after a declined card is always safe. Earlier pending attempts for the same "
            "booking are marked abandoned.\n\n"
            "The amount is always computed server-side as the booking's outstanding "
            "balance. After payment, call `GET /api/payments/verify/{reference}/`."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["booking_id"],
            properties={
                "booking_id": openapi.Schema(
                    type=openapi.TYPE_STRING, format=openapi.FORMAT_UUID,
                    description="UUID of the Booking (from POST /api/bookings/ response).",
                ),
            },
        ),
        responses={
            200: openapi.Response("Transaction initialized.", schema=_init_response),
            400: openapi.Response("Booking already paid / not payable.", schema=_error_schema),
            404: openapi.Response("Booking not found.", schema=_error_schema),
            502: openapi.Response("Paystack gateway error.", schema=_error_schema),
        },
    )
    def post(self, request):
        serializer = InitializePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking_id = serializer.validated_data["booking_id"]
        intent = serializer.validated_data.get("intent", "balance")
        channel = serializer.validated_data.get("channel", "card")
        momo_phone = serializer.validated_data.get("momo_phone")
        momo_provider = serializer.validated_data.get("momo_provider")

        try:
            booking = Booking.objects.select_related("package").get(id=booking_id)
        except Booking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status in (Booking.Status.CANCELLED, Booking.Status.COMPLETED):
            return Response(
                {"detail": f"This booking is {booking.status} and can no longer be paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if booking.is_paid:
            return Response({"detail": "This booking has already been paid in full."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Lazy expiry: an unpaid option-based booking past its TTL cannot be
        # revived — the customer rebooks at current pricing (this also bounds
        # how long a locked early-bird price can sit unpaid).
        if booking.is_expired:
            return Response(
                {"detail": "This booking has expired. Please create a new booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Server-computed amount, whatever the intent ──────────────────────
        balance = booking.balance
        if intent == "deposit":
            if booking.payment_plan != Booking.PaymentPlan.INSTALLMENT or not booking.deposit_required:
                return Response({"detail": "This booking has no deposit plan."},
                                status=status.HTTP_400_BAD_REQUEST)
            outstanding_deposit = quantize(booking.deposit_required) - booking.amount_paid
            if outstanding_deposit <= 0:
                return Response({"detail": "The deposit has already been paid."},
                                status=status.HTTP_400_BAD_REQUEST)
            amount = min(outstanding_deposit, balance)
        elif intent == "custom":
            # Clamp, never trust: a top-up can never exceed the outstanding balance.
            amount = min(quantize(serializer.validated_data["amount"]), balance)
        else:
            amount = balance

        if booking.payment_plan == Booking.PaymentPlan.INSTALLMENT:
            purpose = (
                Payment.Purpose.DEPOSIT
                if booking.amount_paid < (booking.deposit_required or 0)
                else Payment.Purpose.INSTALLMENT
            )
        else:
            purpose = Payment.Purpose.FULL if booking.amount_paid == 0 else Payment.Purpose.INSTALLMENT

        # ── Gateway conversion ───────────────────────────────────────────────
        # Paystack Ghana charges GHS only. Non-GHS bookings (USD flagship) are
        # charged in GHS at the package's admin-set rate, snapshotted on this
        # payment; the ledger amount stays in the booking currency.
        charged_amount = None
        charged_currency = None
        exchange_rate = None
        if booking.currency != "GHS":
            from .fx import FxUnavailable, effective_charge_rate

            try:
                exchange_rate, rate_source = effective_charge_rate(booking.package)
            except FxUnavailable:
                logger.error(
                    "No usable FX rate for non-GHS package %s.", booking.package_id,
                )
                from .alerts import alert_admin
                alert_admin(
                    "fx-unavailable",
                    "FX unavailable — checkout payments are being refused",
                    f"No live, recent or manual exchange rate for package "
                    f"{booking.package_id} ({booking.currency}->GHS). Customers "
                    "cannot pay until a rate is available. Set the manual "
                    "fallback rate in admin or check the rate providers.",
                )
                return Response(
                    {"detail": "Payment is temporarily unavailable. Please try again later."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            charged_amount = quantize(amount * exchange_rate)
            charged_currency = "GHS"
            logger.info(
                "FX charge for booking %s: %s %s -> %s GHS @ %s (%s)",
                booking.reference, amount, booking.currency,
                charged_amount, exchange_rate, rate_source,
            )

        payment = Payment.objects.create(
            booking=booking,
            paystack_reference=generate_payment_reference(booking),
            amount=amount,
            currency=booking.currency,
            charged_amount=charged_amount,
            charged_currency=charged_currency,
            exchange_rate=exchange_rate,
            purpose=purpose,
        )

        gateway_amount = to_subunits(charged_amount if charged_amount is not None else amount)
        gateway_currency = charged_currency or booking.currency
        gateway_metadata = {
            "booking_reference": booking.reference,
            "customer_name": booking.full_name,
            "package": booking.package.title,
        }
        payment_prompt = None

        try:
            if channel == "momo" and momo_provider in DIRECT_MOMO_PROVIDERS:
                # Direct charge: Paystack pushes the approve-prompt to the
                # buyer's phone. No redirect, no popup — the frontend shows the
                # prompt copy and polls /payments/status/.
                paystack_data = charge_mobile_money(
                    email=booking.email,
                    amount_kobo=gateway_amount,
                    reference=payment.paystack_reference,
                    currency=gateway_currency,
                    phone=momo_phone,
                    provider=momo_provider,
                    metadata=gateway_metadata,
                )
                if paystack_data.get("status") == "failed":
                    raise ValueError(paystack_data.get("gateway_response") or "Charge failed.")
                payment_prompt = paystack_data.get("display_text") or (
                    "Approve the payment prompt on your phone to complete this payment. "
                    "If no prompt appears, dial your network's approval shortcode."
                )
            else:
                # Hosted checkout / Inline popup. The buyer's explicit method
                # choice restricts what Paystack shows: Telecel MoMo goes
                # through the hosted mobile-money page (its direct charge needs
                # Paystack's own OTP UI); card gets a card-only page.
                channels = ["mobile_money"] if channel == "momo" else ["card"]
                paystack_data = initialize_transaction(
                    email=booking.email,
                    amount_kobo=gateway_amount,
                    reference=payment.paystack_reference,
                    currency=gateway_currency,
                    metadata=gateway_metadata,
                    channels=channels,
                )
        except Exception as exc:
            logger.exception("Paystack init failed for booking %s: %s", booking_id, exc)
            payment.status = Payment.Status.FAILED
            payment.review_note = "Gateway initialization failed before checkout."
            payment.save(update_fields=["status", "review_note", "updated_at"])
            return Response({"detail": "Payment gateway error. Please try again later."},
                            status=status.HTTP_502_BAD_GATEWAY)

        payment.paystack_access_code = paystack_data.get("access_code")
        payment.paystack_authorization_url = paystack_data.get("authorization_url")
        payment.gateway_response = paystack_data
        payment.save(update_fields=["paystack_access_code", "paystack_authorization_url",
                                    "gateway_response", "updated_at"])

        # Shrink the duplicate-payment window: retire other still-pending
        # gateway attempts for this booking (their references stay reserved).
        (
            Payment.objects.filter(booking=booking, status=Payment.Status.PENDING)
            .exclude(pk=payment.pk)
            .update(status=Payment.Status.ABANDONED)
        )

        return Response({
            "authorization_url": payment.paystack_authorization_url,
            "access_code": payment.paystack_access_code,
            "channel": channel,
            "payment_prompt": payment_prompt,
            "reference": payment.paystack_reference,
            "amount": str(amount),
            "currency": booking.currency,
            "charged_amount": str(charged_amount) if charged_amount is not None else None,
            "charged_currency": charged_currency,
            "exchange_rate": str(exchange_rate) if exchange_rate is not None else None,
            "booking_reference": booking.reference,
        }, status=status.HTTP_200_OK)


# ── 1b. Payment channels ─────────────────────────────────────────────────────

class PaymentChannelsView(APIView):
    """The payment methods the checkout offers, server-controlled so routing
    changes (e.g. a network moving between direct-charge and hosted) never
    need a frontend deploy. The buyer's explicit choice is authoritative —
    networks are never inferred from phone prefixes."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            "channels": [
                {"code": "momo", "label": "Mobile Money",
                 "providers": [
                     {"code": "mtn", "label": "MTN Mobile Money", "direct": True},
                     {"code": "atl", "label": "AT Money (AirtelTigo)", "direct": True},
                     {"code": "vod", "label": "Telecel Cash", "direct": False},
                 ]},
                {"code": "card", "label": "Card (Visa / Mastercard)", "providers": []},
            ],
        })


# ── 2. Verify ─────────────────────────────────────────────────────────────────

class VerifyPaymentView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payments"

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_verify",
        operation_summary="Verify a Paystack payment (one gateway call)",
        operation_description=(
            "Confirms the payment with Paystack **once** and applies it to the booking. "
            "Call this a single time after the Paystack redirect; for subsequent polling "
            "use `GET /api/payments/status/{reference}/`, which never hits the gateway.\n\n"
            "**Safe to retry** — application is idempotent and races cleanly with the webhook."
        ),
        manual_parameters=[
            openapi.Parameter("reference", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True,
                              description="Payment reference returned by /initialize/."),
        ],
        responses={
            200: openapi.Response("Verification result.", schema=_status_response),
            404: openapi.Response("Payment reference not found.", schema=_error_schema),
            502: openapi.Response("Paystack gateway error.", schema=_error_schema),
        },
    )
    def get(self, request, reference):
        try:
            payment = Payment.objects.select_related("booking").get(paystack_reference=reference)
        except Payment.DoesNotExist:
            return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)

        if payment.status == Payment.Status.SUCCESS:
            return Response(_payment_status_payload(payment), status=status.HTTP_200_OK)

        try:
            paystack_data = verify_transaction(reference)
        except Exception as exc:
            logger.exception("Paystack verify failed for ref %s: %s", reference, exc)
            return Response({"detail": "Payment gateway error. Please try again."},
                            status=status.HTTP_502_BAD_GATEWAY)

        paystack_status = paystack_data.get("status")

        if paystack_status == "success":
            result = apply_successful_payment(payment, paystack_data)
            payment = result.payment
        elif paystack_status in ("failed", "abandoned"):
            payment = mark_payment_unsuccessful(payment, paystack_status, paystack_data)

        payment.refresh_from_db()
        payment.booking.refresh_from_db()
        return Response(_payment_status_payload(payment), status=status.HTTP_200_OK)


# ── 3. Status (DB-only poll target) ──────────────────────────────────────────

class PaymentStatusView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment-status"

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_status",
        operation_summary="Poll payment status (no gateway call)",
        operation_description=(
            "Returns the payment's current state straight from the database. "
            "Use this for polling after checkout — the webhook (or the one-shot "
            "verify call) is what moves the state."
        ),
        manual_parameters=[
            openapi.Parameter("reference", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True),
        ],
        responses={
            200: openapi.Response("Current status.", schema=_status_response),
            404: openapi.Response("Payment reference not found.", schema=_error_schema),
        },
    )
    def get(self, request, reference):
        try:
            payment = Payment.objects.select_related("booking").get(paystack_reference=reference)
        except Payment.DoesNotExist:
            return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_payment_status_payload(payment), status=status.HTTP_200_OK)


# ── 4. Webhook ────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=["Payments"],
        operation_id="payment_webhook",
        operation_summary="Paystack webhook (server-to-server only)",
        operation_description=(
            "⚠️ **Do NOT call this from your frontend.** Called by Paystack's servers.\n\n"
            "**Security**: the `X-Paystack-Signature` HMAC-SHA512 header is validated "
            "before any data is processed.\n\n"
            "Returns 200 once the event is processed (or is not ours); returns 5xx on "
            "processing errors so Paystack retries delivery."
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
        reference = data.get("reference")
        logger.info("Paystack webhook: event=%s ref=%s", event_type, reference)

        if event_type not in ("charge.success", "charge.failed") or not reference:
            return Response(status=status.HTTP_200_OK)

        try:
            payment = Payment.objects.select_related("booking").get(paystack_reference=reference)
        except Payment.DoesNotExist:
            logger.info("Webhook: ref %s not found, skipping.", reference)
            return Response(status=status.HTTP_200_OK)

        try:
            if event_type == "charge.success":
                # Re-confirm with the gateway before applying (signature proves
                # the sender; verify proves the money): the webhook payload is
                # treated as a notification, never as the source of truth.
                verified = verify_transaction(reference)
                if verified.get("status") != "success":
                    logger.warning(
                        "Webhook said success but verify says %r for ref %s — not applied.",
                        verified.get("status"), reference,
                    )
                    return Response(status=status.HTTP_200_OK)
                result = apply_successful_payment(payment, verified)
                if result.applied:
                    logger.info("Webhook applied ref=%s booking=%s promoted=%s problems=%s",
                                reference, result.booking.reference, result.promoted, result.problems)
            else:  # charge.failed
                mark_payment_unsuccessful(payment, "failed", data)
        except Exception:
            # Deliberate 5xx: Paystack retries delivery, so a transient DB
            # error does not lose the event.
            logger.exception("Webhook processing failed for ref %s", reference)
            return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(status=status.HTTP_200_OK)
