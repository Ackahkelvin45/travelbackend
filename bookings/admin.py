from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import ChoicesDropdownFilter, RangeDateFilter, RelatedDropdownFilter
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminTextareaWidget

from config.unfold_theme import BOOKING_STAGE_BADGE, BOOKING_STATUS_BADGE, LEAD_STAGE_BADGE, PAYMENT_STATE_BADGE
from payments.models import Payment
from payments.services import record_offline_payment
from .models import Booking, BookingTraveller, CancellationRequest, Lead, PolicyAcceptance, PolicyDocument


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


CHECKLIST = [
    ("names", "Guest names confirmed"),
    ("tickets", "Core ticket count matches guests"),
    ("rooms", "Room allocation confirmed"),
    ("supplier", "Supplier availability confirmed"),
    ("travel", "Travel information received"),
    ("deadlines", "Payment deadlines explained"),
    ("contacted", "Lead guest contacted"),
]


class QueueFilter(admin.SimpleListFilter):
    """The work queues behind the bookings tabs (Change Request v2 admin)."""
    title = "queue"
    parameter_name = "queue"

    def lookups(self, request, model_admin):
        return [("needs_coordinator", "Needs coordinator"), ("contact_overdue", "Contact overdue (24 h)"),
                ("balance_due", "Balance due"),
                ("paid", "Paid in full"), ("pending", "Pending payment"), ("cancelled", "Cancelled")]

    def queryset(self, request, queryset):
        from django.db.models import F
        from payments.money import FULLY_PAID_TOLERANCE
        owed = F("amount_paid") - F("amount_refunded") + FULLY_PAID_TOLERANCE
        v = self.value()
        if v == "needs_coordinator":
            return queryset.filter(status=Booking.Status.CONFIRMED, coordinator__isnull=True)
        if v == "contact_overdue":
            from config.dashboard import contact_overdue_ids
            return queryset.filter(pk__in=contact_overdue_ids())
        if v == "balance_due":
            return queryset.filter(status=Booking.Status.CONFIRMED, total_amount__gt=owed)
        if v == "paid":
            return queryset.filter(status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED], total_amount__lte=owed)
        if v == "pending":
            return queryset.filter(status=Booking.Status.PENDING)
        if v == "cancelled":
            return queryset.filter(status=Booking.Status.CANCELLED)
        return queryset


class StayFilter(admin.SimpleListFilter):
    title = "stay"
    parameter_name = "stay"

    def lookups(self, request, model_admin):
        from packages.models import StayType
        seen = dict(StayType.objects.order_by("kind", "order").values_list("code", "name"))
        return [*seen.items(), ("own", "Own stay")]

    def queryset(self, request, queryset):
        if self.value() == "own":
            return queryset.exclude(mode="").filter(room_units__isnull=True)
        if self.value():
            return queryset.filter(room_units__stay_type__code=self.value()).distinct()
        return queryset


class BookingAdminForm(forms.ModelForm):
    checklist_items = forms.MultipleChoiceField(
        choices=CHECKLIST, required=False, label="Confirmation checklist",
        widget=forms.CheckboxSelectMultiple,
        help_text="Within 24 hours of payment. Ticking 'Lead guest contacted' records the contact time.",
    )

    class Meta:
        model = Booking
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["checklist_items"].initial = [k for k, v in (self.instance.checklist or {}).items() if v]

    def save(self, commit=True):
        from django.utils import timezone
        booking = super().save(commit=False)
        ticked = set(self.cleaned_data.get("checklist_items") or [])
        booking.checklist = {k: k in ticked for k, _ in CHECKLIST}
        if "contacted" in ticked and not booking.lead_contacted_at:
            booking.lead_contacted_at = timezone.now()
        if "coordinator" in self.changed_data:
            booking.coordinator_assigned_at = timezone.now() if booking.coordinator_id else None
        if commit:
            booking.save()
        return booking


class TravellerInline(TabularInline):
    """One row per Core ticket. Guests can be renamed or moved between this
    booking's rooms (capacity enforced); tickets are never added or removed here."""
    model = BookingTraveller
    extra = 0
    tab = True
    verbose_name = "Guest"
    verbose_name_plural = "Guests & rooms"
    fields = ["full_name", "is_lead", "ticket", "unit", "experiences_display", "travel_info"]
    readonly_fields = ["is_lead", "ticket", "experiences_display", "travel_info"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "unit":
            from packages.models import RoomUnit
            object_id = request.resolver_match.kwargs.get("object_id")
            kwargs["queryset"] = RoomUnit.objects.filter(booking_id=object_id).select_related("stay_type")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Core ticket")
    def ticket(self, obj):
        index = list(obj.booking.travellers.values_list("pk", flat=True)).index(obj.pk) + 1
        return f"{obj.booking.reference}-T{index}"

    @admin.display(description="Experiences")
    def experiences_display(self, obj):
        names = dict(obj.booking.package.addons.values_list("code", "name"))
        return " · ".join(names.get(c, c) for c in obj.experiences) or "—"

    @admin.display(description="Travel info")
    def travel_info(self, obj):
        if not obj.travel_info_submitted_at:
            return "Not started"
        bits = [obj.arrival_flight and f"Flight {obj.arrival_flight}", obj.dietary and f"Diet: {obj.dietary}",
                obj.emergency_contact and f"Emergency: {obj.emergency_contact} {obj.emergency_phone}"]
        return " · ".join(b for b in bits if b) or "Submitted"


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    form = BookingAdminForm
    actions = ["assign_coordinator", "send_balance_reminders", "export_csv",
               "mark_arrived", "cancel_with_refund", "cancel_as_organizer", "mark_completed"]
    actions_detail = ["detail_send_reminder", "detail_resend_confirmation", "detail_mark_contacted"]
    list_display = ["reference_cell", "lead_cell", "stay_cell", "total_cell", "paid_cell",
                    "due_cell", "coordinator_cell", "stage_badge"]
    list_filter = [
        QueueFilter,
        StayFilter,
        ("coordinator", RelatedDropdownFilter),
        ("status", ChoicesDropdownFilter),
        ("mode", ChoicesDropdownFilter),
        ("payment_plan", ChoicesDropdownFilter),
        ("travel_date", RangeDateFilter),
        ("cancellation_reason", ChoicesDropdownFilter),
        OverdueCureFilter,
    ]
    list_filter_submit = True
    search_fields = ["reference", "email", "first_name", "last_name", "phone", "travellers__full_name"]
    list_select_related = ["coordinator", "package"]
    change_form_before_template = "admin/bookings/booking_summary.html"
    readonly_fields = [
        "reference", "option", "option_snapshot", "unit_price", "total_amount",
        "early_bird_applied", "early_bird_discount", "addons", "discount_amount", "discount_note",
        "price_list_version", "quote_snapshot",
        "refund_tiers_snapshot", "deposit_required",
        "amount_paid", "amount_refunded", "balance_display", "payment_state_display",
        "effective_deadline_display", "cancelled_at", "cancellation_reason",
        "reminders_sent", "overdue_notice_sent_at", "cure_deadline_display",
        "arrived_at", "created_at", "updated_at",
        "coordinator_assigned_at", "lead_contacted_at", "contact_sla",
    ]
    ordering = ["-created_at"]
    inlines = [TravellerInline, PaymentInline, CancellationRequestInline, PolicyAcceptanceInline]
    fieldsets = (
        ("Overview", {
            "classes": ["tab"],
            "fields": ("reference", "status", "user", ("first_name", "last_name"), ("email", "phone"),
                       "country", ("mode", "num_guests"), "package", "option", "travel_date",
                       "arrived_at", "special_requests"),
        }),
        ("Coordinator", {
            "classes": ["tab"],
            "fields": ("coordinator", "coordinator_assigned_at", "contact_sla", "lead_contacted_at", "checklist_items"),
        }),
        ("Money", {
            "classes": ["tab"],
            "description": "Snapshot fields are what the customer bought — they never "
                           "change after booking. Paid/balance are cached from the "
                           "payment ledger.",
            "fields": (
                "payment_plan", "price_list_version", "unit_price", "total_amount", "currency",
                "discount_amount", "discount_note", "early_bird_applied", "early_bird_discount", "addons",
                "deposit_required", "amount_paid", "amount_refunded",
                "balance_display", "payment_state_display",
            ),
        }),
        ("Deadlines", {
            "classes": ["tab"],
            "fields": ("payment_deadline_override", "payment_deadline_override_reason",
                       "effective_deadline_display", "reminders_sent",
                       "overdue_notice_sent_at", "cure_deadline_display"),
        }),
        ("Consent", {
            "classes": ["tab"],
            "description": "Separate from the accepted policies below. Guests can change these in My Booking.",
            "fields": ("marketing_consent", "photo_consent"),
        }),
        ("Cancellation", {
            "classes": ["tab"],
            "fields": ("cancelled_at", "cancellation_reason", "refund_tiers_snapshot"),
        }),
        ("Snapshots", {
            "classes": ["tab"],
            "fields": ("quote_snapshot", "option_snapshot", "created_at", "updated_at"),
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

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "coordinator":
            from django.contrib.auth import get_user_model
            kwargs["queryset"] = get_user_model().objects.filter(is_staff=True, is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        # Policy: every deadline extension is recorded in writing.
        if "payment_deadline_override" in form.changed_data and obj.payment_deadline_override \
                and not obj.payment_deadline_override_reason:
            from django.contrib import messages
            self.message_user(request, "Deadline extended without a reason — please add one.", messages.WARNING)
        super().save_model(request, obj, form, change)

    # ── List columns ────────────────────────────────────────────────────────
    @display(description="Reference", ordering="created_at", header=True)
    def reference_cell(self, obj):
        return obj.reference, obj.created_at.strftime("%d %b %Y")

    @display(description="Lead guest", header=True)
    def lead_cell(self, obj):
        return obj.full_name, (obj.get_mode_display() if obj.mode else obj.email)

    @admin.display(description="Stay")
    def stay_cell(self, obj):
        units = [u.stay_type.name for u in obj.room_units.select_related("stay_type")]
        if units:
            from collections import Counter
            return ", ".join(f"{n} × {c}" if c > 1 else n for n, c in Counter(units).items())
        return "Own stay" if obj.mode else (obj.option_snapshot or {}).get("hotel_name", "—")

    @admin.display(description="Total", ordering="total_amount")
    def total_cell(self, obj):
        return f"{obj.currency} {obj.total_amount:,.2f}"

    @admin.display(description="Paid")
    def paid_cell(self, obj):
        from django.utils.html import format_html
        pct = min(100, int((obj.amount_paid - obj.amount_refunded) / obj.total_amount * 100)) if obj.total_amount else 0
        return format_html(
            '<div class="az-paid"><span>{} {}</span><span class="az-bar"><span style="width:{}%"></span></span></div>',
            obj.currency, f"{obj.amount_paid:,.2f}", max(pct, 0),
        )

    @display(description="Balance due", header=True)
    def due_cell(self, obj):
        if obj.status == Booking.Status.CANCELLED or obj.is_paid:
            return "—", ""
        deadline = obj.effective_payment_deadline
        return f"{obj.currency} {obj.balance:,.2f}", (f"by {deadline:%d %b %Y}" if deadline else "")

    @admin.display(description="Coordinator", ordering="coordinator__first_name")
    def coordinator_cell(self, obj):
        return obj.coordinator.full_name or obj.coordinator.email if obj.coordinator else "Unassigned"

    @staticmethod
    def _stage(obj):
        if obj.status == Booking.Status.CANCELLED:
            return "cancelled", "Cancelled"
        if obj.status == Booking.Status.COMPLETED:
            return "completed", "Completed"
        if obj.status == Booking.Status.PENDING:
            return "pending", "Pending payment"
        if not obj.coordinator_id:
            return "coordinator", "Needs coordinator"
        return ("paid", "Paid in full") if obj.is_paid else ("deposit", "Deposit paid")

    @display(description="Status", label=BOOKING_STAGE_BADGE)
    def stage_badge(self, obj):
        return self._stage(obj)

    # ── Detail read-only helpers ────────────────────────────────────────────
    @admin.display(description="Paid")
    def amount_paid_display(self, obj):
        return f"{obj.amount_paid} {obj.currency}"

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return f"{obj.balance} {obj.currency}"

    @admin.display(description="Cure period ends")
    def cure_deadline_display(self, obj):
        return obj.cure_deadline or "—"

    @admin.display(description="24-hour contact")
    def contact_sla(self, obj):
        return contact_sla(obj)[1]

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

    @admin.display(description="Effective payment deadline")
    def effective_deadline_display(self, obj):
        return obj.effective_payment_deadline or "—"

    # ── Bulk actions ────────────────────────────────────────────────────────
    @admin.action(description="Assign coordinator")
    def assign_coordinator(self, request, queryset):
        from django.contrib import messages
        from django.contrib.auth import get_user_model
        from django.template.response import TemplateResponse
        from django.utils import timezone

        staff = get_user_model().objects.filter(is_staff=True, is_active=True).order_by("first_name")
        if "apply" in request.POST:
            user = staff.filter(pk=request.POST.get("coordinator")).first()
            if user is None:
                self.message_user(request, "Choose a coordinator.", messages.WARNING)
                return None
            n = queryset.update(coordinator=user, coordinator_assigned_at=timezone.now())
            self.message_user(request, f"{n} booking(s) assigned to {user.full_name or user.email}.", messages.SUCCESS)
            return None
        return TemplateResponse(request, "admin/bookings/assign_coordinator.html", {
            **self.admin_site.each_context(request), "title": "Assign coordinator",
            "bookings": queryset, "staff": staff, "opts": self.model._meta,
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        })

    @admin.action(description="Send balance reminder (skips paid and cancelled)")
    def send_balance_reminders(self, request, queryset):
        from django.contrib import messages
        from .services import send_balance_reminder
        sent = sum(1 for b in queryset.select_related("package") if send_balance_reminder(b))
        skipped = queryset.count() - sent
        self.message_user(request, f"{sent} reminder(s) sent, {skipped} skipped (paid, cancelled or no deadline).",
                          messages.SUCCESS if sent else messages.WARNING)

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        import csv
        from django.http import HttpResponse
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="bookings.csv"'
        w = csv.writer(response)
        w.writerow(["Reference", "Created", "Lead guest", "Email", "Phone", "Mode", "Guests", "Stay", "Status",
                    "Currency", "Total", "Paid", "Refunded", "Balance", "Balance due", "Coordinator"])
        for b in queryset.select_related("coordinator", "package"):
            deadline = b.effective_payment_deadline
            w.writerow([b.reference, b.created_at.date(), b.full_name, b.email, b.phone or "", b.mode, b.num_guests,
                        self.stay_cell(b), self._stage(b)[1], b.currency, b.total_amount, b.amount_paid,
                        b.amount_refunded, b.balance, deadline or "", self.coordinator_cell(b)])
        return response

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

    # ── Detail buttons ──────────────────────────────────────────────────────
    def _back(self, object_id):
        from django.shortcuts import redirect
        return redirect("admin:bookings_booking_change", object_id)

    @action(description="Send balance reminder now", url_path="send-reminder")
    def detail_send_reminder(self, request, object_id):
        from django.contrib import messages
        from .services import send_balance_reminder
        booking = Booking.objects.get(pk=object_id)
        if send_balance_reminder(booking):
            self.message_user(request, f"Balance reminder sent to {booking.email}.", messages.SUCCESS)
        else:
            self.message_user(request, "No reminder sent: the booking is paid, not confirmed or has no deadline.", messages.WARNING)
        return self._back(object_id)

    @action(description="Resend confirmation and receipt", url_path="resend-confirmation")
    def detail_resend_confirmation(self, request, object_id):
        from django.contrib import messages
        from payments.email import send_booking_confirmation
        booking = Booking.objects.get(pk=object_id)
        payment = booking.payments.filter(status=Payment.Status.SUCCESS).order_by("-paid_at").first()
        if payment is None:
            self.message_user(request, "No successful payment yet, so there is no confirmation to resend.", messages.WARNING)
        else:
            send_booking_confirmation(booking, payment)
            self.message_user(request, f"Confirmation and receipt resent to {booking.email}.", messages.SUCCESS)
        return self._back(object_id)

    @action(description="Mark lead guest contacted", url_path="mark-contacted")
    def detail_mark_contacted(self, request, object_id):
        from django.contrib import messages
        from django.utils import timezone
        booking = Booking.objects.get(pk=object_id)
        booking.lead_contacted_at = booking.lead_contacted_at or timezone.now()
        booking.checklist = {**(booking.checklist or {}), "contacted": True}
        booking.save(update_fields=["lead_contacted_at", "checklist", "updated_at"])
        self.message_user(request, "Contact recorded.", messages.SUCCESS)
        return self._back(object_id)

    def save_formset(self, request, form, formset, change):
        """Payments: route new rows through the offline-payment service.
        Travellers: room moves go through move_traveller (capacity enforced,
        never a second ticket) and renames are recorded in traveller_changes."""
        if formset.model is BookingTraveller:
            return self._save_travellers(request, form.instance, formset)
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

    def _save_travellers(self, request, booking, formset):
        from django.contrib import messages
        from django.utils import timezone
        from .services import IllegalTransition, move_traveller

        changes = []
        for f in formset.forms:
            if not f.has_changed() or not f.instance.pk:
                continue
            original = BookingTraveller.objects.get(pk=f.instance.pk)
            if "full_name" in f.changed_data:
                changes.append({"kind": "name", "from": original.full_name, "to": f.cleaned_data["full_name"],
                                "at": timezone.now().isoformat(), "by": request.user.email})
                BookingTraveller.objects.filter(pk=original.pk).update(full_name=f.cleaned_data["full_name"])
            if "unit" in f.changed_data:
                try:
                    move_traveller(original, f.cleaned_data["unit"])
                except IllegalTransition as exc:
                    self.message_user(request, f"{original.full_name}: {exc}", messages.ERROR)
        if changes:
            Booking.objects.filter(pk=booking.pk).update(traveller_changes=[*(booking.traveller_changes or []), *changes])


def contact_sla(booking):
    """(state, text) for the 24-hour coordinator contact rule, from first payment."""
    from datetime import timedelta
    from django.utils import timezone
    from payments.models import OpsConfig

    if booking.lead_contacted_at:
        return "done", f"Contacted {booking.lead_contacted_at:%d %b %H:%M}"
    first = booking.payments.filter(status=Payment.Status.SUCCESS).order_by("paid_at").first()
    if not first or not first.paid_at:
        return "waiting", "Starts when the first payment lands"
    due = first.paid_at + timedelta(hours=OpsConfig.get().coordinator_contact_hours)
    left = due - timezone.now()
    if left.total_seconds() < 0:
        return "late", f"Overdue by {abs(int(left.total_seconds() // 3600))} h (due {due:%d %b %H:%M})"
    return "due", f"{int(left.total_seconds() // 3600)} h {int(left.total_seconds() % 3600 // 60)} min left (due {due:%d %b %H:%M})"


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


class ReplyFilter(admin.SimpleListFilter):
    title = "reply"
    parameter_name = "reply"

    def lookups(self, request, model_admin):
        return [("overdue", "Reply overdue"), ("waiting", "Waiting for reply"), ("replied", "Replied")]

    def queryset(self, request, queryset):
        from django.utils import timezone
        if self.value() == "overdue":
            return queryset.filter(replied_at__isnull=True, reply_due_at__lt=timezone.now()).exclude(stage__in=["won", "lost"])
        if self.value() == "waiting":
            return queryset.filter(replied_at__isnull=True).exclude(stage__in=["won", "lost"])
        if self.value() == "replied":
            return queryset.filter(replied_at__isnull=False)
        return queryset


def lead_reply_state(lead):
    """(state, text) for the reply column: replied / overdue / due / closed."""
    from django.utils import timezone
    if lead.replied_at:
        return "replied", "Replied"
    if lead.stage in (Lead.Stage.WON, Lead.Stage.LOST):
        return "closed", "—"
    if not lead.reply_due_at:
        return "due", "Waiting"
    left = lead.reply_due_at - timezone.now()
    mins = int(abs(left.total_seconds()) // 60)
    span = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    return ("overdue", f"Overdue {span}") if left.total_seconds() < 0 else ("due", f"Due in {span}")


@admin.register(Lead)
class LeadAdmin(ModelAdmin):
    """Leads & quotes (canvas: DjLeads). Reminders only go to people who
    consented, and never after they booked or opted out."""
    list_display = ["lead_cell", "source", "quote_cell", "consent_cell", "reply_cell", "stage_badge", "assigned_to"]
    list_filter = [("stage", ChoicesDropdownFilter), ("source", ChoicesDropdownFilter), ReplyFilter,
                   "marketing_consent", ("assigned_to", RelatedDropdownFilter)]
    list_filter_submit = True
    search_fields = ["name", "email", "phone", "referral_code", "message"]
    list_select_related = ["assigned_to", "booking", "package"]
    actions = ["mark_replied", "assign_to_me", "mark_lost", "export_csv"]
    actions_detail = ["detail_mark_replied", "detail_send_reminder"]
    readonly_fields = ["timeline", "quote_summary", "booking", "created_at", "updated_at"]
    fieldsets = (
        ("Lead", {"classes": ["tab"], "fields": (("name", "email"), "phone", "source", "message", "referral_code",
                                                  "package", "booking")}),
        ("Follow-up", {"classes": ["tab"], "fields": ("stage", "assigned_to", "reply_due_at", "replied_at", "notes", "timeline")}),
        ("Quote & consent", {"classes": ["tab"], "fields": ("quote_summary", "marketing_consent")}),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "assigned_to":
            from django.contrib.auth import get_user_model
            kwargs["queryset"] = get_user_model().objects.filter(is_staff=True, is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(description="Lead", header=True, ordering="created_at")
    def lead_cell(self, obj):
        return obj.name, obj.email or obj.phone

    @admin.display(description="Quote")
    def quote_cell(self, obj):
        q = obj.quote or {}
        return f"{q.get('currency', 'USD')} {float(q['total']):,.2f}" if q.get("total") else "—"

    @admin.display(description="Consent")
    def consent_cell(self, obj):
        return "Marketing" if obj.marketing_consent else "Reply only"

    @display(description="Reply", label={"overdue": "danger", "due": "warning", "replied": "success", "closed": "info"})
    def reply_cell(self, obj):
        return lead_reply_state(obj)

    @display(description="Stage", label=LEAD_STAGE_BADGE)
    def stage_badge(self, obj):
        return obj.stage, obj.get_stage_display()

    @admin.display(description="Saved quote")
    def quote_summary(self, obj):
        from django.utils.html import format_html, format_html_join
        q = obj.quote or {}
        if not q:
            return "No saved quote."
        rows = [("Mode", str(q.get("mode", "")).title()), ("Travellers", q.get("num_guests")),
                ("Rooms", ", ".join(r["name"] for r in q.get("rooms", [])) or "Own stay"),
                ("Savings", f"{q.get('currency')} {q.get('discount_total')}"),
                ("Total", f"{q.get('currency')} {q.get('total')}"), ("Price list", q.get("price_list_version") or "draft")]
        return format_html('<table class="az-kv">{}</table>',
                           format_html_join("", "<tr><th>{}</th><td>{}</td></tr>", rows))

    @admin.display(description="Timeline")
    def timeline(self, obj):
        from django.utils.html import format_html, format_html_join
        events = [(obj.created_at, f"{obj.get_source_display()} enquiry received")]
        events += [(e.created_at, f"Email '{e.kind}' {e.status}") for e in obj.emails.all()]
        if obj.replied_at:
            events.append((obj.replied_at, "Marked replied"))
        if obj.booking_id:
            events.append((obj.booking.created_at, f"Booked {obj.booking.reference} — sales reminders stopped"))
        return format_html('<ul class="az-timeline">{}</ul>', format_html_join(
            "", "<li><span>{}</span>{}</li>", ((t.strftime("%d %b %Y %H:%M"), text) for t, text in sorted(events))))

    @admin.action(description="Mark replied")
    def mark_replied(self, request, queryset):
        from django.utils import timezone
        n = queryset.filter(replied_at__isnull=True).update(replied_at=timezone.now())
        queryset.filter(stage=Lead.Stage.NEW).update(stage=Lead.Stage.CONTACTED)
        self.message_user(request, f"{n} lead(s) marked replied.")

    @admin.action(description="Assign to me")
    def assign_to_me(self, request, queryset):
        self.message_user(request, f"{queryset.update(assigned_to=request.user)} lead(s) assigned to you.")

    @admin.action(description="Mark lost")
    def mark_lost(self, request, queryset):
        self.message_user(request, f"{queryset.exclude(stage=Lead.Stage.WON).update(stage=Lead.Stage.LOST)} lead(s) closed as lost.")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        import csv
        from django.http import HttpResponse
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="leads.csv"'
        w = csv.writer(response)
        w.writerow(["Created", "Name", "Email", "Phone", "Source", "Stage", "Quote total", "Marketing consent",
                    "Referral code", "Reply due", "Replied", "Booking"])
        for l in queryset.select_related("booking"):
            w.writerow([l.created_at.date(), l.name, l.email, l.phone, l.get_source_display(), l.get_stage_display(),
                        (l.quote or {}).get("total", ""), l.marketing_consent, l.referral_code,
                        l.reply_due_at or "", l.replied_at or "", l.booking.reference if l.booking_id else ""])
        return response

    def _back(self, object_id):
        from django.shortcuts import redirect
        return redirect("admin:bookings_lead_change", object_id)

    @action(description="Mark replied", url_path="mark-replied")
    def detail_mark_replied(self, request, object_id):
        from django.utils import timezone
        lead = Lead.objects.get(pk=object_id)
        lead.replied_at = lead.replied_at or timezone.now()
        lead.stage = Lead.Stage.CONTACTED if lead.stage == Lead.Stage.NEW else lead.stage
        lead.save(update_fields=["replied_at", "stage", "updated_at"])
        self.message_user(request, "Marked replied.")
        return self._back(object_id)

    @action(description="Send quote reminder", url_path="send-reminder")
    def detail_send_reminder(self, request, object_id):
        from django.contrib import messages
        from payments.email import send_saved_quote
        lead = Lead.objects.get(pk=object_id)
        if lead.stage in (Lead.Stage.WON, Lead.Stage.LOST) or lead.booking_id:
            self.message_user(request, "Not sent: this lead has booked or is closed, so sales reminders are stopped.", messages.WARNING)
        elif not lead.marketing_consent:
            self.message_user(request, "Not sent: no marketing consent. Reply only to their direct enquiry.", messages.WARNING)
        elif not lead.quote:
            self.message_user(request, "Not sent: this lead has no saved quote.", messages.WARNING)
        else:
            send_saved_quote(lead)
            self.message_user(request, f"Quote reminder sent to {lead.email}.", messages.SUCCESS)
        return self._back(object_id)
