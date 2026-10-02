from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import ChoicesDropdownFilter, RangeDateFilter
from unfold.decorators import display
from unfold.widgets import UnfoldAdminTextareaWidget

from config.unfold_theme import BOOKING_STATUS_BADGE, PAYMENT_STATE_BADGE
from payments.models import Payment
from payments.services import record_offline_payment
from .models import Booking, CancellationRequest, PolicyAcceptance, PolicyDocument


class OfflinePaymentInlineForm(forms.ModelForm):
    """New rows added here are routed through payments.services — the same
    path Paystack payments take — so confirmation, receipts and cached totals
    behave identically. Gateway payments can never be hand-entered."""

    class Meta:
        model = Payment
        fields = ["amount", "method", "purpose", "note"]

    def clean_method(self):
        method = self.cleaned_data["method"]
        if method == Payment.Method.PAYSTACK:
            raise forms.ValidationError(
                "Paystack payments are created by checkout, not by hand. "
                "Use 'Bank transfer' or 'Other' for offline payments."
            )
        return method

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("Amount must be positive.")
        return amount


class PaymentInline(TabularInline):
    model = Payment
    form = OfflinePaymentInlineForm
    extra = 0
    verbose_name = "Payment"
    verbose_name_plural = "Payments (add a row to record an offline payment)"
    fields = ["amount", "currency", "purpose", "method", "status", "needs_review", "paid_at", "note"]
    readonly_fields = ["currency", "status", "needs_review", "paid_at"]
    ordering = ["-created_at"]

    def has_change_permission(self, request, obj=None):
        return False  # existing ledger rows are immutable in admin

    def has_delete_permission(self, request, obj=None):
        return False


class PolicyAcceptanceInline(TabularInline):
    model = PolicyAcceptance
    extra = 0
    fields = ["document", "email", "ip_address", "accepted_at"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OverdueCureFilter(admin.SimpleListFilter):
    """Installment Policy: surfaces bookings whose overdue notice went out and
    whose cure period has lapsed — the staff decision point (no auto-cancel)."""
    title = "overdue cure"
    parameter_name = "cure"

    def lookups(self, request, model_admin):
        return [("notice_sent", "Notice sent, in cure period"), ("expired", "Cure period expired")]

    def queryset(self, request, queryset):
        from datetime import timedelta
        from django.utils import timezone
        from .models import OVERDUE_CURE_HOURS
        cutoff = timezone.now() - timedelta(hours=OVERDUE_CURE_HOURS)
        if self.value() == "notice_sent":
            return queryset.filter(status=Booking.Status.CONFIRMED, overdue_notice_sent_at__gt=cutoff)
        if self.value() == "expired":
            return queryset.filter(status=Booking.Status.CONFIRMED, overdue_notice_sent_at__lte=cutoff)
        return queryset


class CancellationRequestInline(TabularInline):
    model = CancellationRequest
    extra = 0
    fields = ["requested_at", "status", "reason", "resolved_at", "resolved_by"]
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    actions = ["mark_arrived", "cancel_with_refund", "cancel_as_organizer", "mark_completed"]
    list_display = [
        "reference", "full_name", "email", "package", "payment_plan",
        "total_amount", "amount_paid_display", "balance_display",
        "payment_state_badge", "currency", "status_badge", "created_at",
    ]
    list_filter = [
        ("status", ChoicesDropdownFilter),
        ("payment_plan", ChoicesDropdownFilter),
        "currency",
        ("travel_date", RangeDateFilter),
        ("cancellation_reason", ChoicesDropdownFilter),
        OverdueCureFilter,
    ]
    list_filter_submit = True
    search_fields = ["reference", "email", "first_name", "last_name", "phone"]
    readonly_fields = [
        "reference", "option", "option_snapshot", "unit_price", "total_amount",
        "early_bird_applied", "early_bird_discount", "addons",
        "refund_tiers_snapshot", "deposit_required",
        "amount_paid", "amount_refunded", "balance_display", "payment_state_display",
        "effective_deadline_display", "cancelled_at", "cancellation_reason",
        "reminders_sent", "overdue_notice_sent_at", "cure_deadline_display",
        "arrived_at", "created_at", "updated_at",
    ]
    ordering = ["-created_at"]
    inlines = [PaymentInline, CancellationRequestInline, PolicyAcceptanceInline]
    fieldsets = (
        ("Booking Reference", {
            "fields": ("reference", "status"),
        }),
        ("Guest Info", {
            "fields": ("user", "first_name", "last_name", "email", "phone", "country"),
        }),
        ("Trip Details", {
            "fields": ("package", "option", "num_guests", "travel_date", "arrived_at", "special_requests"),
        }),
        ("Money", {
            "description": "Snapshot fields are what the customer bought — they never "
                           "change after booking. Paid/balance are cached from the "
                           "payment ledger below.",
            "fields": (
                "payment_plan", "unit_price", "total_amount", "currency",
                "early_bird_applied", "early_bird_discount", "addons",
                "deposit_required", "amount_paid", "amount_refunded",
                "balance_display", "payment_state_display",
            ),
        }),
        ("Deadlines", {
            "fields": ("payment_deadline_override", "payment_deadline_override_reason",
                       "effective_deadline_display", "reminders_sent",
                       "overdue_notice_sent_at", "cure_deadline_display"),
        }),
        ("Cancellation", {
            "fields": ("cancelled_at", "cancellation_reason", "refund_tiers_snapshot"),
        }),
        ("Snapshot Detail", {
            "classes": ("collapse",),
            "fields": ("option_snapshot",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )

    # ── Staff permissions (three levels, see setup_staff_groups) ───────────
    def has_cancel_booking_permission(self, request):
        return request.user.has_perm("bookings.cancel_booking")

    def has_cancel_as_organizer_permission(self, request):
        return request.user.has_perm("bookings.cancel_as_organizer")

    def has_mark_arrived_permission(self, request):
        return request.user.has_perm("bookings.mark_arrived")

    def get_fieldsets(self, request, obj=None):
        """Special requests may contain health data — only shown with the
        dedicated permission (Privacy Policy, Part D)."""
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.has_perm("bookings.view_special_requests"):
            return fieldsets
        return tuple(
            (name, {**opts, "fields": tuple(f for f in opts["fields"] if f != "special_requests")})
            for name, opts in fieldsets
        )

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm("bookings.extend_deadline"):
            ro += ["payment_deadline_override", "payment_deadline_override_reason"]
        return ro

    def save_model(self, request, obj, form, change):
        # Policy: every deadline extension is recorded in writing.
        if "payment_deadline_override" in form.changed_data and obj.payment_deadline_override \
                and not obj.payment_deadline_override_reason:
            from django.contrib import messages
            self.message_user(request, "Deadline extended without a reason — please add one.", messages.WARNING)
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark arrived / tour started (no refund on voluntary cancellation)",
                  permissions=["mark_arrived"])
    def mark_arrived(self, request, queryset):
        from django.contrib import messages
        from django.utils import timezone
        updated = queryset.filter(arrived_at__isnull=True).update(arrived_at=timezone.now())
        self.message_user(request, f"{updated} booking(s) marked arrived.", messages.SUCCESS)

    @admin.action(description="Cancel as ORGANIZER — full refund of everything paid",
                  permissions=["cancel_as_organizer"])
    def cancel_as_organizer(self, request, queryset):
        from django.contrib import messages
        from .services import IllegalTransition, cancel_booking
        done = 0
        for booking in queryset:
            try:
                cancel_booking(booking, reason=Booking.CancellationReason.ORGANIZER,
                               refund_reason="Organizer cancellation — full refund")
                done += 1
            except (IllegalTransition, ValueError) as exc:
                self.message_user(request, f"{booking.reference}: {exc}", messages.WARNING)
        if done:
            self.message_user(request, f"{done} booking(s) cancelled with FULL refund legs pending in Refunds.", messages.SUCCESS)

    @admin.display(description="Paid")
    def amount_paid_display(self, obj):
        return f"{obj.amount_paid} {obj.currency}"

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return f"{obj.balance} {obj.currency}"

    @admin.display(description="Cure period ends")
    def cure_deadline_display(self, obj):
        return obj.cure_deadline or "—"

    @staticmethod
    def _payment_state(obj):
        """(key, human label) — keys match PAYMENT_STATE_BADGE."""
        if obj.is_paid:
            return "paid", "Fully paid"
        if obj.is_overdue:
            return "overdue", "Overdue"
        if obj.amount_paid > 0:
            return "partial", "Partially paid"
        if obj.is_expired:
            return "expired", "Expired (unpaid)"
        return "unpaid", "Unpaid"

    @admin.display(description="Payment state")
    def payment_state_display(self, obj):
        return self._payment_state(obj)[1]

    @display(description="Payment state", label=PAYMENT_STATE_BADGE)
    def payment_state_badge(self, obj):
        return self._payment_state(obj)

    @display(description="Status", ordering="status", label=BOOKING_STATUS_BADGE)
    def status_badge(self, obj):
        return obj.status, obj.get_status_display()

    @admin.display(description="Effective payment deadline")
    def effective_deadline_display(self, obj):
        return obj.effective_payment_deadline or "—"

    @admin.action(description="Cancel booking + compute refund", permissions=["cancel_booking"])
    def cancel_with_refund(self, request, queryset):
        from django.contrib import messages

        from .services import IllegalTransition, cancel_booking

        done, skipped = 0, 0
        for booking in queryset:
            try:
                cancel_booking(booking, reason=Booking.CancellationReason.ADMIN)
                done += 1
            except (IllegalTransition, ValueError) as exc:
                skipped += 1
                self.message_user(request, f"{booking.reference}: {exc}", messages.WARNING)
        if done:
            self.message_user(
                request,
                f"{done} booking(s) cancelled. Pending refund legs (if any) are in "
                "the Refunds admin — execute them and mark processed.",
                messages.SUCCESS,
            )

    @admin.action(description="Mark completed (trip has taken place)")
    def mark_completed(self, request, queryset):
        from django.contrib import messages

        updated = queryset.filter(status=Booking.Status.CONFIRMED).update(
            status=Booking.Status.COMPLETED
        )
        self.message_user(request, f"{updated} booking(s) marked completed.", messages.SUCCESS)

    def save_formset(self, request, form, formset, change):
        """Route inline-added payment rows through the offline-payment service
        instead of saving them raw — never write ledger rows by hand."""
        if formset.model is not Payment:
            return super().save_formset(request, form, formset, change)

        instances = formset.save(commit=False)
        for obj in instances:
            if obj.pk is None:
                record_offline_payment(
                    form.instance,
                    amount=obj.amount,
                    method=obj.method,
                    purpose=obj.purpose,
                    note=(obj.note or "") + f" [recorded by {request.user.email}]",
                )
        formset.save_m2m()


class PolicyDocumentForm(forms.ModelForm):
    class Meta:
        model = PolicyDocument
        fields = "__all__"
        widgets = {"body": UnfoldAdminTextareaWidget(attrs={"rows": 24})}


@admin.register(PolicyDocument)
class PolicyDocumentAdmin(ModelAdmin):
    form = PolicyDocumentForm
    list_display = ["type", "version", "title", "is_required", "is_current", "published_at", "created_at"]
    list_filter = [("type", ChoicesDropdownFilter), "is_current", "is_required"]
    search_fields = ["title", "body"]
    ordering = ["type", "-created_at"]
    actions = ["publish_documents"]

    def get_readonly_fields(self, request, obj=None):
        # Published text is evidence of what customers accepted — frozen.
        if obj and obj.published_at:
            return ["type", "version", "body", "published_at", "created_at"]
        return ["published_at", "created_at"]

    @admin.action(description="Publish and make current")
    def publish_documents(self, request, queryset):
        from django.contrib import messages
        from django.utils import timezone

        for doc in queryset.filter(published_at__isnull=True):
            doc.published_at = timezone.now()
            doc.is_current = True
            doc.save()
        self.message_user(request, "Selected documents published.", messages.SUCCESS)


@admin.register(CancellationRequest)
class CancellationRequestAdmin(ModelAdmin):
    """Approve = cancel the booking as of the request time and create refund
    legs (Payments → Refunds). Reject = booking stays, payments reopen. The
    guest is emailed either way; the staff note is included."""
    list_display = ["booking", "requested_at", "status", "quoted_refund", "resolved_at", "resolved_by"]
    list_filter = [("status", ChoicesDropdownFilter)]
    search_fields = ["booking__reference", "booking__email"]
    readonly_fields = ["booking", "requested_at", "reason", "status", "refund_quote", "resolved_at", "resolved_by"]
    fields = ["booking", "requested_at", "reason", "status", "refund_quote", "staff_note", "resolved_at", "resolved_by"]
    actions = ["approve", "reject"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Quoted refund")
    def quoted_refund(self, obj):
        return f"{(obj.refund_quote or {}).get('refund_total', '—')} {obj.booking.currency}"

    def _resolve(self, request, queryset, fn, verb):
        from django.contrib import messages
        from .services import IllegalTransition
        done = 0
        for req in queryset.select_related("booking"):
            try:
                fn(req, by_user=request.user, note=req.staff_note)
                done += 1
            except IllegalTransition as exc:
                self.message_user(request, f"{req.booking.reference}: {exc}", messages.WARNING)
        if done:
            self.message_user(request, f"{done} request(s) {verb}.", messages.SUCCESS)

    def has_resolve_cancellation_permission(self, request):
        return request.user.has_perm("bookings.resolve_cancellation")

    @admin.action(description="Approve — cancel booking and create refund legs",
                  permissions=["resolve_cancellation"])
    def approve(self, request, queryset):
        from .services import approve_cancellation
        self._resolve(request, queryset, approve_cancellation, "approved")

    @admin.action(description="Reject — keep booking, reopen payments",
                  permissions=["resolve_cancellation"])
    def reject(self, request, queryset):
        from .services import reject_cancellation
        self._resolve(request, queryset, reject_cancellation, "rejected")
