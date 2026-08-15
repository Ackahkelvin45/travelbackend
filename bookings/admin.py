from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import ChoicesDropdownFilter, RangeDateFilter
from unfold.decorators import display
from unfold.widgets import UnfoldAdminTextareaWidget

from config.unfold_theme import BOOKING_STATUS_BADGE, PAYMENT_STATE_BADGE
from payments.models import Payment
from payments.services import record_offline_payment
from .models import Booking, PolicyAcceptance, PolicyDocument


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


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    actions = ["cancel_with_refund", "mark_completed"]
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
    ]
    list_filter_submit = True
    search_fields = ["reference", "email", "first_name", "last_name", "phone"]
    readonly_fields = [
        "reference", "option", "option_snapshot", "unit_price", "total_amount",
        "early_bird_applied", "early_bird_discount", "addons",
        "refund_tiers_snapshot", "deposit_required",
        "amount_paid", "amount_refunded", "balance_display", "payment_state_display",
        "effective_deadline_display", "cancelled_at", "cancellation_reason",
        "reminders_sent", "created_at", "updated_at",
    ]
    ordering = ["-created_at"]
    inlines = [PaymentInline, PolicyAcceptanceInline]
    fieldsets = (
        ("Booking Reference", {
            "fields": ("reference", "status"),
        }),
        ("Guest Info", {
            "fields": ("user", "first_name", "last_name", "email", "phone", "country"),
        }),
        ("Trip Details", {
            "fields": ("package", "option", "num_guests", "travel_date", "special_requests"),
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
            "fields": ("payment_deadline_override", "effective_deadline_display", "reminders_sent"),
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

    @admin.display(description="Paid")
    def amount_paid_display(self, obj):
        return f"{obj.amount_paid} {obj.currency}"

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return f"{obj.balance} {obj.currency}"

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

    @admin.action(description="Cancel booking + compute refund")
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
    list_display = ["type", "version", "title", "is_current", "published_at", "created_at"]
    list_filter = [("type", ChoicesDropdownFilter), "is_current"]
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
