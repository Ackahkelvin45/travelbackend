"""
Staff portal API — /api/staff/…

Purpose-built for the Azura operations team (non-technical), NOT for
developers: bookings, payments and the day-to-day actions their permission
level allows. Every endpoint declares `required_perm`; StaffPermission
enforces it, and the frontend reads /staff/me/ to show only what's allowed.
"""

from datetime import date

from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.models import Payment
from .models import Booking
from .serializers import BookingDetailSerializer
from .staff_permissions import StaffPermission

# The capabilities the portal cares about, in the order they're granted.
CAPABILITIES = [
    "bookings.view_booking", "bookings.view_special_requests", "bookings.change_booking", "payments.add_payment",
    "bookings.extend_deadline", "bookings.mark_arrived", "bookings.resolve_cancellation",
    "bookings.cancel_booking", "bookings.cancel_as_organizer", "payments.process_refund",
]


def _owned(reference):
    return Booking.objects.select_related("package", "user").filter(reference=reference).first()


# ── Who am I / what can I do ─────────────────────────────────────────────────

class StaffMeView(APIView):
    permission_classes = [StaffPermission]
    required_perm = "bookings.view_booking"

    def get(self, request):
        u = request.user
        return Response({
            "email": u.email,
            "name": f"{u.first_name} {u.last_name}".strip(),
            "groups": list(u.groups.values_list("name", flat=True)),
            "perms": [p for p in CAPABILITIES if u.has_perm(p)],
        })


# ── Bookings list ────────────────────────────────────────────────────────────

class StaffBookingListSerializer(serializers.ModelSerializer):
    package_title = serializers.CharField(source="package.title")
    balance = serializers.SerializerMethodField()
    payment_state = serializers.SerializerMethodField()
    payment_deadline = serializers.SerializerMethodField()
    is_overdue = serializers.BooleanField()
    has_pending_cancellation = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "reference", "first_name", "last_name", "email", "package_title", "travel_date",
            "num_guests", "total_amount", "amount_paid", "balance", "currency", "payment_state",
            "status", "is_overdue", "arrived_at", "payment_deadline", "has_pending_cancellation",
            "created_at",
        ]

    def get_balance(self, obj):
        return str(obj.balance)

    def get_payment_state(self, obj):
        if obj.is_paid:
            return "fully_paid"
        return "partially_paid" if obj.amount_paid > 0 else "unpaid"

    def get_payment_deadline(self, obj):
        return obj.effective_payment_deadline

    def get_has_pending_cancellation(self, obj):
        return obj.pending_cancellation is not None


class StaffBookingListView(ListAPIView):
    """?q= reference / name / email / tour · ?status= · ?payment_state=
    unpaid|partially_paid|fully_paid · ?overdue=1 · ?arrived=1 · ?pending_cancellation=1"""

    permission_classes = [StaffPermission]
    required_perm = "bookings.view_booking"
    serializer_class = StaffBookingListSerializer

    def get_queryset(self):
        p = self.request.query_params
        qs = (
            Booking.objects.select_related("package")
            .annotate(deadline=Coalesce("payment_deadline_override", "package__final_payment_deadline"))
            .order_by("-created_at")
        )
        if q := p.get("q", "").strip():
            qs = qs.filter(
                Q(reference__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                | Q(email__icontains=q) | Q(package__title__icontains=q)
            )
        if s := p.get("status"):
            qs = qs.filter(status=s)
        state = p.get("payment_state")
        if state == "unpaid":
            qs = qs.filter(amount_paid=0)
        elif state == "partially_paid":
            qs = qs.filter(amount_paid__gt=0, amount_paid__lt=F("total_amount"))
        elif state == "fully_paid":
            qs = qs.filter(amount_paid__gte=F("total_amount"))
        if p.get("overdue") == "1":
            qs = qs.filter(status=Booking.Status.CONFIRMED, deadline__lt=date.today(),
                           amount_paid__lt=F("total_amount"))
        if p.get("arrived") == "1":
            qs = qs.filter(arrived_at__isnull=False)
        if p.get("pending_cancellation") == "1":
            qs = qs.filter(cancellation_requests__status="pending").distinct()
        return qs


# ── Booking detail ───────────────────────────────────────────────────────────

class StaffBookingSerializer(BookingDetailSerializer):
    """Everything the guest sees, plus what staff need. `special_requests`
    (may hold health data) only appears with bookings.view_special_requests."""

    account_email = serializers.EmailField(source="user.email", read_only=True, default=None)
    is_overdue = serializers.BooleanField(read_only=True)
    cancellation_requests = serializers.SerializerMethodField()
    refunds = serializers.SerializerMethodField()
    policy_acceptances = serializers.SerializerMethodField()

    days_to_departure = serializers.SerializerMethodField()

    class Meta(BookingDetailSerializer.Meta):
        fields = BookingDetailSerializer.Meta.fields + [
            "special_requests", "account_email", "arrived_at", "is_overdue", "days_to_departure",
            "traveller_changes", "addon_changes", "addons",
            "payment_deadline_override", "payment_deadline_override_reason", "reminders_sent",
            "cancellation_requests", "refunds", "policy_acceptances",
        ]
        read_only_fields = fields

    def to_representation(self, obj):
        data = super().to_representation(obj)
        user = self.context["request"].user
        if not user.has_perm("bookings.view_special_requests"):
            data.pop("special_requests", None)
        return data

    def get_days_to_departure(self, obj):
        return (obj.travel_date - date.today()).days

    def get_cancellation_requests(self, obj):
        return [
            {"id": str(r.id), "status": r.status, "requested_at": r.requested_at, "reason": r.reason,
             "refund_total": (r.refund_quote or {}).get("refund_total"),
             "percent": (r.refund_quote or {}).get("percent"),
             "resolved_at": r.resolved_at, "staff_note": r.staff_note}
            for r in obj.cancellation_requests.all()
        ]

    def get_refunds(self, obj):
        return [
            {"id": str(r.id), "amount": str(r.amount), "currency": r.currency, "status": r.status,
             "gateway_amount": (r.breakdown or {}).get("execute_on_gateway"),
             "processed_at": r.processed_at, "external_reference": r.external_reference,
             "requested_at": r.requested_at}
            for r in obj.refunds.all()
        ]

    def get_policy_acceptances(self, obj):
        return [
            {"type": a.document.type, "title": a.document.title, "version": a.document.version,
             "accepted_at": a.accepted_at}
            for a in obj.policy_acceptances.select_related("document")
        ]


class StaffBookingDetailView(APIView):
    permission_classes = [StaffPermission]
    required_perm = "bookings.view_booking"

    def get(self, request, reference):
        booking = _owned(reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(StaffBookingSerializer(booking, context={"request": request}).data)


# ── Actions ──────────────────────────────────────────────────────────────────

class _StaffAction(APIView):
    permission_classes = [StaffPermission]

    def booking_or_404(self, reference):
        booking = _owned(reference)
        if not booking:
            return None, Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return booking, None

    def detail(self, request, booking):
        booking.refresh_from_db()
        return Response(StaffBookingSerializer(booking, context={"request": request}).data)


class StaffOfflinePaymentView(_StaffAction):
    """POST {amount, method: bank_transfer|other, purpose?, note} — records a
    payment received outside Paystack through the SAME path gateway payments
    take (confirmation, receipt email, cached totals)."""
    required_perm = "payments.add_payment"

    def post(self, request, reference):
        from decimal import Decimal, InvalidOperation

        from payments.services import record_offline_payment

        booking, err = self.booking_or_404(reference)
        if err:
            return err
        if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
            return Response({"detail": f"A {booking.status} booking cannot take payments."}, status=400)
        try:
            amount = Decimal(str(request.data.get("amount", "")))
        except (InvalidOperation, ValueError):
            return Response({"detail": "Enter a valid amount."}, status=400)
        if amount <= 0 or amount > booking.balance:
            return Response({"detail": f"Amount must be between 0.01 and the balance ({booking.currency} {booking.balance})."}, status=400)
        method = request.data.get("method", Payment.Method.BANK_TRANSFER)
        if method not in (Payment.Method.BANK_TRANSFER, Payment.Method.OTHER):
            return Response({"detail": "Method must be bank_transfer or other."}, status=400)
        purpose = request.data.get("purpose") or None
        if purpose and purpose not in Payment.Purpose.values:
            return Response({"detail": "Unknown payment purpose."}, status=400)
        note = f"{str(request.data.get('note', '')).strip()} [recorded by {request.user.email}]".strip()
        try:
            record_offline_payment(booking, amount=amount, method=method, note=note, purpose=purpose)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return self.detail(request, booking)


class StaffExtendDeadlineView(_StaffAction):
    """POST {date: YYYY-MM-DD, reason} — per-booking deadline override.
    Policy: every extension is recorded in writing, so `reason` is required."""
    required_perm = "bookings.extend_deadline"

    def post(self, request, reference):
        booking, err = self.booking_or_404(reference)
        if err:
            return err
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            return Response({"detail": "A reason for the extension is required."}, status=400)
        try:
            new_date = date.fromisoformat(str(request.data.get("date", "")))
        except ValueError:
            return Response({"detail": "Enter a valid date (YYYY-MM-DD)."}, status=400)
        if new_date < date.today():
            return Response({"detail": "The new deadline cannot be in the past."}, status=400)
        booking.payment_deadline_override = new_date
        booking.payment_deadline_override_reason = f"{reason} — {request.user.email}"[:300]
        booking.save(update_fields=["payment_deadline_override", "payment_deadline_override_reason", "updated_at"])
        return self.detail(request, booking)


class StaffMarkArrivedView(_StaffAction):
    """POST — the guest has arrived / the tour has started for them. From now
    on voluntary cancellation refunds nothing (Refund Policy arrival rule)."""
    required_perm = "bookings.mark_arrived"

    def post(self, request, reference):
        booking, err = self.booking_or_404(reference)
        if err:
            return err
        if not booking.arrived_at:
            booking.arrived_at = timezone.now()
            booking.save(update_fields=["arrived_at", "updated_at"])
        return self.detail(request, booking)


# ── Cancellation queue (Operations) ─────────────────────────────────────────

def _request_row(r):
    b = r.booking
    q = r.refund_quote or {}
    return {
        "id": str(r.id), "status": r.status, "requested_at": r.requested_at, "reason": r.reason,
        "resolved_at": r.resolved_at, "staff_note": r.staff_note,
        "refund_total": q.get("refund_total"), "percent": q.get("percent"),
        "days_before_departure": q.get("days_before_departure"),
        "booking": {
            "reference": b.reference, "first_name": b.first_name, "last_name": b.last_name, "email": b.email,
            "package_title": b.package.title, "travel_date": b.travel_date, "status": b.status,
            "amount_paid": str(b.amount_paid), "balance": str(b.balance), "currency": b.currency,
            "arrived_at": b.arrived_at,
        },
    }


class StaffCancellationListView(APIView):
    """GET /api/staff/cancellations/?status=pending|approved|rejected|all (default pending)."""
    permission_classes = [StaffPermission]
    required_perm = "bookings.view_cancellationrequest"

    def get(self, request):
        from .models import CancellationRequest
        qs = CancellationRequest.objects.select_related("booking__package").order_by("requested_at")
        st = request.query_params.get("status", "pending")
        if st != "all":
            qs = qs.filter(status=st)
        return Response([_request_row(r) for r in qs[:200]])


class StaffCancellationResolveView(APIView):
    """POST /api/staff/cancellations/<id>/approve/ or /reject/ {note}.
    Approve = booking cancelled as of the request time, refund legs created,
    guest emailed. Reject = booking stays, payments reopen, guest emailed."""
    permission_classes = [StaffPermission]
    required_perm = "bookings.resolve_cancellation"

    def post(self, request, pk, decision):
        from .models import CancellationRequest
        from .services import IllegalTransition, approve_cancellation, reject_cancellation

        req = CancellationRequest.objects.select_related("booking__package").filter(pk=pk).first()
        if not req:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        note = str(request.data.get("note", "")).strip()[:2000]
        fn = {"approve": approve_cancellation, "reject": reject_cancellation}.get(decision)
        if fn is None:
            return Response({"detail": "Unknown decision."}, status=400)
        try:
            fn(req, by_user=request.user, note=note)
        except IllegalTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.refresh_from_db()
        return Response(_request_row(req))


# ── Refunds to execute (Finance) ─────────────────────────────────────────────

def _refund_row(r):
    b = r.booking
    return {
        "id": str(r.id), "status": r.status, "amount": str(r.amount), "currency": r.currency,
        "reason": r.reason, "requested_at": r.requested_at, "processed_at": r.processed_at,
        "external_reference": r.external_reference, "execution_note": r.execution_note,
        "gateway_instruction": (r.breakdown or {}).get("execute_on_gateway"),
        "payment_reference": r.payment.paystack_reference if r.payment else None,
        "payment_method": r.payment.method if r.payment else None,
        "basis": (r.breakdown or {}).get("tier_applied", {}).get("basis"),
        "percent": (r.breakdown or {}).get("percent"),
        "booking": {
            "reference": b.reference, "first_name": b.first_name, "last_name": b.last_name,
            "email": b.email, "package_title": b.package.title, "currency": b.currency,
        },
    }


class StaffRefundListView(APIView):
    """GET /api/staff/refunds/?status=pending|processed|rejected|all (default pending)."""
    permission_classes = [StaffPermission]
    required_perm = "payments.view_refund"

    def get(self, request):
        from payments.models import Refund
        qs = Refund.objects.select_related("booking__package", "payment").order_by("requested_at")
        st = request.query_params.get("status", "pending")
        if st != "all":
            qs = qs.filter(status=st)
        return Response([_refund_row(r) for r in qs[:200]])


class StaffRefundResolveView(APIView):
    """POST /api/staff/refunds/<id>/process/ {external_reference, note} — the
    money has been sent (Paystack dashboard / bank); records it, updates the
    booking's refunded total and emails the guest the amount + reference.
    POST …/reject/ {note} — leg will not be paid."""
    permission_classes = [StaffPermission]
    required_perm = "payments.process_refund"

    def post(self, request, pk, decision):
        from payments.models import Refund
        from payments.refunds import mark_refund_processed

        refund = Refund.objects.select_related("booking__package", "payment").filter(pk=pk).first()
        if not refund:
            return Response({"detail": "Refund not found."}, status=status.HTTP_404_NOT_FOUND)
        note = str(request.data.get("note", "")).strip()[:2000]
        if decision == "process":
            ref = str(request.data.get("external_reference", "")).strip()[:100]
            if not ref:
                return Response({"detail": "Enter the refund reference (Paystack refund id or bank transfer reference)."}, status=400)
            try:
                mark_refund_processed(
                    refund, by_user=request.user, external_reference=ref,
                    execution_note=f"{note} [via staff portal by {request.user.email}]".strip(),
                )
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=400)
        elif decision == "reject":
            if refund.status != Refund.Status.PENDING:
                return Response({"detail": "Only pending refunds can be rejected."}, status=400)
            refund.status = Refund.Status.REJECTED
            refund.execution_note = f"Rejected: {note} [by {request.user.email}]"
            refund.save(update_fields=["status", "execution_note", "updated_at"])
        else:
            return Response({"detail": "Unknown decision."}, status=400)
        refund.refresh_from_db()
        return Response(_refund_row(refund))


# ── Email history + cancellation by staff ────────────────────────────────────

class StaffBookingEmailsView(APIView):
    """GET /api/staff/bookings/<reference>/emails/ — what we sent this guest."""
    permission_classes = [StaffPermission]
    required_perm = "bookings.view_booking"

    def get(self, request, reference):
        booking = _owned(reference)
        if not booking:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response([
            {"id": str(e.id), "kind": e.kind, "to": e.to_email, "subject": e.subject, "status": e.status,
             "provider_id": e.provider_id, "error": e.error, "sent_at": e.created_at}
            for e in booking.emails.all()[:100]
        ])


class StaffCancelBookingView(_StaffAction):
    """POST {mode: "guest" | "organizer", note} — cancel a booking.
    guest     → Refund Policy bands apply (needs bookings.cancel_booking)
    organizer → we couldn't deliver: FULL refund (needs bookings.cancel_as_organizer)"""
    required_perm = "bookings.view_booking"   # the real check is per mode, below

    def post(self, request, reference):
        from .services import IllegalTransition, cancel_booking

        booking, err = self.booking_or_404(reference)
        if err:
            return err
        mode = request.data.get("mode")
        perm, reason = {
            "guest": ("bookings.cancel_booking", Booking.CancellationReason.ADMIN),
            "organizer": ("bookings.cancel_as_organizer", Booking.CancellationReason.ORGANIZER),
        }.get(mode, (None, None))
        if perm is None:
            return Response({"detail": "mode must be 'guest' or 'organizer'."}, status=400)
        if not request.user.has_perm(perm):
            return Response({"detail": "You don't have permission to do that."}, status=status.HTTP_403_FORBIDDEN)
        note = str(request.data.get("note", "")).strip()[:300]
        try:
            cancel_booking(
                booking, reason=reason,
                refund_reason=(f"{'Organizer cancellation — full refund' if mode == 'organizer' else 'Cancelled by staff'}"
                               f"{': ' + note if note else ''} [{request.user.email}]"),
            )
        except (IllegalTransition, ValueError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return self.detail(request, booking)


# ── Name correction / traveller replacement (Terms Part A) ───────────────────

TRANSFER_CUTOFF_DAYS = 14


class StaffTravellerChangeView(_StaffAction):
    """POST {kind: "correction" | "replacement", first_name, last_name, email?, phone?, note?}

    correction  → same person, fixed spelling. Free.
    replacement → a different person takes the place. Free; allowed up to
                  TRANSFER_CUTOFF_DAYS before departure — later requests need
                  individual review, so a note is required. If the email
                  changes, the booking is detached from the old account (ownership
                  never follows an email change automatically) and the new
                  traveller is emailed a fresh link to claim it.
    Both identities, who approved and when are appended to traveller_changes.
    """
    required_perm = "bookings.change_booking"

    def post(self, request, reference):
        booking, err = self.booking_or_404(reference)
        if err:
            return err
        if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
            return Response({"detail": f"A {booking.status} booking cannot be changed."}, status=400)

        kind = request.data.get("kind")
        if kind not in ("correction", "replacement"):
            return Response({"detail": "kind must be 'correction' or 'replacement'."}, status=400)
        first = str(request.data.get("first_name", "")).strip()[:100]
        last = str(request.data.get("last_name", "")).strip()[:100]
        if not first or not last:
            return Response({"detail": "First and last name are required."}, status=400)
        email = str(request.data.get("email", "") or booking.email).strip().lower()[:254]
        phone = str(request.data.get("phone", "") or booking.phone or "").strip()[:20]
        note = str(request.data.get("note", "")).strip()[:500]

        days = (booking.travel_date - date.today()).days
        if kind == "replacement" and days < TRANSFER_CUTOFF_DAYS and not note:
            return Response(
                {"detail": f"Fewer than {TRANSFER_CUTOFF_DAYS} days to departure — replacements now need individual "
                           "review. Add a note explaining the approval."},
                status=400,
            )

        previous = {"first_name": booking.first_name, "last_name": booking.last_name,
                    "email": booking.email, "phone": booking.phone}
        new = {"first_name": first, "last_name": last, "email": email, "phone": phone or None}
        if previous == new:
            return Response({"detail": "Nothing changed."}, status=400)

        email_changed = email != booking.email.lower()
        booking.first_name, booking.last_name, booking.email, booking.phone = first, last, email, phone or None
        fields = ["first_name", "last_name", "email", "phone", "traveller_changes", "updated_at"]
        if email_changed and booking.user_id:
            booking.user = None            # the old account must not keep the new traveller's booking
            fields.append("user")
        booking.traveller_changes = [*(booking.traveller_changes or []), {
            "kind": kind, "from": previous, "to": new, "note": note,
            "at": timezone.now().isoformat(), "by": request.user.email,
            "days_to_departure": days,
        }]
        booking.save(update_fields=fields)

        from payments.email import send_traveller_updated
        send_traveller_updated(booking, previous, kind)
        return self.detail(request, booking)


class StaffRemoveAddonView(_StaffAction):
    """POST {code, note?} — cancel one experience/add-on. 50% of its charge is
    kept as a fee; any over-payment becomes a pending refund (Finance)."""
    required_perm = "bookings.cancel_booking"

    def post(self, request, reference):
        from .services import IllegalTransition, remove_addon

        booking, err = self.booking_or_404(reference)
        if err:
            return err
        code = str(request.data.get("code", "")).strip()
        if not code:
            return Response({"detail": "Which add-on?"}, status=400)
        try:
            remove_addon(booking, code=code, by_email=request.user.email,
                         note=str(request.data.get("note", "")).strip()[:300])
        except IllegalTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        return self.detail(request, booking)
