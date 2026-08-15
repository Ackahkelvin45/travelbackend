import json

from django.contrib import admin
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import BooleanRadioFilter, ChoicesDropdownFilter
from unfold.decorators import display

from config.unfold_theme import PAYMENT_STATUS_BADGE, REFUND_STATUS_BADGE
from .models import FxRate, OpsConfig, Payment, Refund, ScheduledTask
from .receipts import compute_line_items, render_receipt_html


# Status → (background, text) colours for the plain-HTML summary card. Mirrors
# the Aurora badge semantics used elsewhere so the at-a-glance state reads the
# same in the list and on the detail page.
_STATUS_COLORS = {
    "success": ("#DCFCE7", "#166534"),
    "pending": ("#FEF9C3", "#854D0E"),
    "failed": ("#FEE2E2", "#991B1B"),
    "abandoned": ("#FEE2E2", "#991B1B"),
    "refunded": ("#DBEAFE", "#1E40AF"),
}


def _money(amount, currency):
    """Human money: 'USD 1,800.00'. Returns '—' for a missing amount."""
    if amount is None:
        return "—"
    return f"{currency or ''} {amount:,.2f}".strip()


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = ["reference_short", "booking", "amount_display", "charged_display", "purpose", "method", "status_badge", "needs_review", "paid_at"]
    list_filter = [
        ("status", ChoicesDropdownFilter),
        ("purpose", ChoicesDropdownFilter),
        ("method", ChoicesDropdownFilter),
        ("needs_review", BooleanRadioFilter),
        "currency",
    ]
    search_fields = ["paystack_reference", "booking__reference", "booking__email"]
    readonly_fields = [
        "payment_overview", "breakdown_table", "receipt_link",
        "gateway_summary", "gateway_response_pretty",
        "id", "booking", "amount", "currency",
        "charged_amount", "charged_currency", "exchange_rate", "purpose", "method",
        "paystack_reference", "paystack_access_code", "paystack_authorization_url",
        "gateway_response", "paid_at", "created_at", "updated_at", "status",
    ]
    ordering = ["-created_at"]
    fieldsets = (
        ("Summary", {
            "description": "Everything you need to understand this payment at a glance.",
            "fields": ("payment_overview",),
        }),
        ("What this paid for", {
            "description": "The booking's priced components. The gateway charge is a lump sum; "
                           "these line items are the snapshot of what the total covers.",
            "fields": ("breakdown_table", "receipt_link"),
        }),
        ("Review", {
            "description": "Set when money arrived but could not be applied automatically — follow up here.",
            "fields": ("needs_review", "review_note", "note"),
        }),
        ("Gateway result", {
            "description": "What Paystack reported for this transaction.",
            "fields": ("gateway_summary",),
        }),
        ("Reference & IDs", {
            "classes": ("collapse",),
            "fields": ("id", "paystack_reference", "paystack_access_code", "paystack_authorization_url"),
        }),
        ("Raw transaction fields", {
            "classes": ("collapse",),
            "fields": ("booking", "amount", "currency",
                       "charged_amount", "charged_currency", "exchange_rate",
                       "purpose", "method", "status", "paid_at", "created_at", "updated_at"),
        }),
        ("Raw gateway response (JSON)", {
            "classes": ("collapse",),
            "fields": ("gateway_response_pretty",),
        }),
    )

    # ── List-view columns ────────────────────────────────────────────────────
    @admin.display(description="Payment", ordering="paystack_reference")
    def reference_short(self, obj):
        return obj.paystack_reference or f"offline:{str(obj.id)[:8]}"

    @admin.display(description="Ledger amount", ordering="amount")
    def amount_display(self, obj):
        return _money(obj.amount, obj.currency)

    @admin.display(description="Charged to gateway")
    def charged_display(self, obj):
        if obj.charged_amount and obj.charged_currency and obj.charged_currency != obj.currency:
            return _money(obj.charged_amount, obj.charged_currency)
        return "—"

    @display(description="Status", ordering="status", label=PAYMENT_STATUS_BADGE)
    def status_badge(self, obj):
        return obj.status, obj.get_status_display()

    # ── Detail-view summary card ─────────────────────────────────────────────
    @admin.display(description="")
    def payment_overview(self, obj):
        booking = obj.booking
        bg, fg = _STATUS_COLORS.get(obj.status, ("#E5E7EB", "#374151"))
        status_pill = format_html(
            '<span style="display:inline-block;padding:3px 12px;border-radius:999px;'
            'background:{};color:{};font-weight:700;font-size:12px;letter-spacing:.3px;">{}</span>',
            bg, fg, obj.get_status_display().upper(),
        )

        # The money story, stated plainly.
        if obj.charged_amount and obj.charged_currency and obj.charged_currency != obj.currency:
            charge_line = format_html(
                "{} &nbsp;<span style='color:#6B7280;'>— charged as</span> <strong>{}</strong> "
                "<span style='color:#6B7280;'>@ {} {}/{}</span>",
                _money(obj.amount, obj.currency), _money(obj.charged_amount, obj.charged_currency),
                obj.exchange_rate, obj.charged_currency, obj.currency,
            )
        else:
            charge_line = format_html("<strong>{}</strong>", _money(obj.amount, obj.currency))

        booking_link = format_html(
            '<a href="{}" style="color:#A17529;font-weight:600;">{}</a>',
            reverse("admin:bookings_booking_change", args=[booking.pk]), booking.reference,
        )
        customer = f"{booking.first_name} {booking.last_name}".strip() or "—"

        gw = obj.gateway_response or {}
        auth = gw.get("authorization") or {}
        card_bits = " · ".join(
            str(b) for b in [
                (auth.get("brand") or "").title() or None,
                f"•••• {auth['last4']}" if auth.get("last4") else None,
                auth.get("bank") or None,
            ] if b
        )
        gateway_msg = gw.get("gateway_response") or gw.get("message") or "—"
        channel = gw.get("channel") or "—"

        rows = [
            ("Status", status_pill),
            ("Amount", charge_line),
            ("Purpose", obj.get_purpose_display()),
            ("Method", obj.get_method_display()),
            ("Customer", format_html("{}<br><span style='color:#6B7280;'>{}</span>", customer, booking.email)),
            ("Booking", booking_link),
            ("Reference", obj.paystack_reference or "—"),
            ("Paid at", obj.paid_at.strftime("%b %d, %Y · %H:%M") if obj.paid_at else "Not paid"),
            ("Gateway said", format_html("{}{}", gateway_msg,
                                         format_html(" &nbsp;<span style='color:#6B7280;'>({} · {})</span>", channel, card_bits) if card_bits else "")),
        ]
        if obj.needs_review:
            rows.append(("⚠ Needs review", format_html("<span style='color:#991B1B;'>{}</span>", obj.review_note or "Flagged for manual follow-up")))

        body = format_html_join(
            "",
            '<tr>'
            '<td style="padding:9px 16px 9px 0;color:#6B7280;font-size:12px;text-transform:uppercase;'
            'letter-spacing:.4px;vertical-align:top;white-space:nowrap;">{}</td>'
            '<td style="padding:9px 0;font-size:14px;">{}</td>'
            '</tr>',
            ((label, value) for label, value in rows),
        )
        return format_html(
            '<table style="border-collapse:collapse;width:100%;max-width:680px;">{}</table>', body,
        )

    @admin.display(description="Gateway result")
    def gateway_summary(self, obj):
        gw = obj.gateway_response or {}
        if not gw:
            return "No gateway response recorded (offline payment or not yet charged)."
        auth = gw.get("authorization") or {}
        fields = [
            ("Result", gw.get("status")),
            ("Message", gw.get("gateway_response") or gw.get("message")),
            ("Channel", gw.get("channel")),
            ("Card", " ".join(str(b) for b in [(auth.get("brand") or "").title(),
                                               f"•••• {auth['last4']}" if auth.get("last4") else ""] if b) or None),
            ("Bank", auth.get("bank")),
            ("Gateway amount", f"{gw.get('currency','')} {gw.get('amount')/100:,.2f}" if isinstance(gw.get("amount"), (int, float)) else None),
            ("Gateway ref", gw.get("reference")),
            ("Gateway paid at", gw.get("paid_at")),
        ]
        body = format_html_join(
            "",
            '<tr><td style="padding:6px 16px 6px 0;color:#6B7280;white-space:nowrap;">{}</td>'
            '<td style="padding:6px 0;font-weight:500;">{}</td></tr>',
            ((label, value) for label, value in fields if value not in (None, "")),
        )
        return format_html('<table style="border-collapse:collapse;font-size:13px;">{}</table>', body)

    @admin.display(description="Raw gateway response")
    def gateway_response_pretty(self, obj):
        if not obj.gateway_response:
            return "—"
        pretty = json.dumps(obj.gateway_response, indent=2, sort_keys=True, default=str)
        return format_html(
            '<pre style="background:#0F172A;color:#E2E8F0;padding:16px;border-radius:8px;'
            'overflow:auto;max-height:420px;font-size:12px;line-height:1.5;">{}</pre>', pretty,
        )

    # ── Component breakdown + receipt ────────────────────────────────────────
    @admin.display(description="Breakdown")
    def breakdown_table(self, obj):
        bd = compute_line_items(obj.booking)
        rows = format_html_join(
            "",
            '<tr><td style="padding:7px 16px 7px 0;">{}<br>'
            '<span style="color:#6B7280;font-size:12px;">{}</span></td>'
            '<td style="padding:7px 0;text-align:right;white-space:nowrap;">{} {}</td></tr>',
            ((l["label"], l["detail"], bd["currency"], f"{l['amount']:,.2f}") for l in bd["lines"]),
        )
        discount = (
            format_html(
                '<tr><td style="padding:7px 0;color:#0a7d3f;">Early-bird saving</td>'
                '<td style="padding:7px 0;text-align:right;color:#0a7d3f;">− {} {}</td></tr>',
                bd["currency"], f"{bd['discount']:,.2f}")
            if bd["discount"] > 0 else ""
        )
        recon = (
            mark_safe('<span style="color:#0a7d3f;">✓ reconciles with booking total</span>')
            if bd["reconciles"] else
            format_html('<span style="color:#991b1b;font-weight:700;">⚠ components ≠ booking total ({} {} vs {} {})</span>',
                        bd["currency"], f"{bd['component_sum']:,.2f}", bd["currency"], f"{bd['total']:,.2f}")
        )
        return format_html(
            '<table style="border-collapse:collapse;width:100%;max-width:560px;font-size:14px;">{}{}'
            '<tr><td style="padding:9px 0;border-top:2px solid #1a1a2e;font-weight:800;">Booking total</td>'
            '<td style="padding:9px 0;border-top:2px solid #1a1a2e;text-align:right;font-weight:800;">{} {}</td></tr>'
            '<tr><td style="padding:5px 0;color:#0a7d3f;">Paid to date</td>'
            '<td style="padding:5px 0;text-align:right;color:#0a7d3f;">{} {}</td></tr>'
            '<tr><td style="padding:5px 0;color:#6B7280;">Balance</td>'
            '<td style="padding:5px 0;text-align:right;">{} {}</td></tr>'
            '</table><div style="margin-top:8px;font-size:12px;">{}</div>',
            rows, discount,
            bd["currency"], f"{bd['total']:,.2f}",
            bd["currency"], f"{bd['amount_paid']:,.2f}",
            bd["currency"], f"{bd['balance']:,.2f}",
            recon,
        )

    @admin.display(description="Receipt")
    def receipt_link(self, obj):
        if obj.status != Payment.Status.SUCCESS:
            return mark_safe('<span style="color:#6B7280;">Receipt available once the payment is successful.</span>')
        url = reverse("admin:payments_payment_receipt", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" target="_blank" '
            'style="display:inline-block;padding:8px 16px;background:#1a1a2e;color:#fff;'
            'border-radius:8px;text-decoration:none;font-weight:600;">⬇ Open / download receipt</a>', url,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("<uuid:pk>/receipt/", self.admin_site.admin_view(self.receipt_view),
                 name="payments_payment_receipt"),
        ]
        return custom + urls

    def receipt_view(self, request, pk):
        # Staff + model view permission required (admin_view already enforces
        # login/staff); object lookup is by UUID pk, not enumerable.
        if not self.has_view_permission(request):
            return HttpResponseForbidden("Not authorized to view payment receipts.")
        payment = get_object_or_404(Payment, pk=pk)
        if payment.status != Payment.Status.SUCCESS:
            return HttpResponse(
                "<p style='font-family:sans-serif;padding:40px;'>No receipt: this payment is "
                f"<strong>{payment.get_status_display()}</strong>, not a completed charge.</p>",
                status=409,
            )
        return HttpResponse(render_receipt_html(payment))

    def has_add_permission(self, request):
        # Payments enter the ledger through checkout or the booking admin's
        # "record offline payment" action — never free-hand rows.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(ModelAdmin):
    actions = ["mark_processed", "reject_refunds"]
    list_display = ["booking", "payment", "amount", "currency", "gateway_instruction", "status_badge", "processed_by", "processed_at", "requested_at"]
    list_filter = [("status", ChoicesDropdownFilter), "currency"]
    search_fields = ["booking__reference", "booking__email", "external_reference"]
    ordering = ["-requested_at"]
    readonly_fields = ["id", "booking", "payment", "amount", "currency", "breakdown", "requested_at", "updated_at"]
    fieldsets = (
        ("Refund", {
            "fields": ("id", "booking", "payment", "amount", "currency", "status", "reason"),
        }),
        ("Computed Breakdown", {
            "description": "Produced by the backend refund calculator — the audit trail for this decision.",
            "fields": ("breakdown",),
        }),
        ("Execution", {
            "description": "Execute the refund in the Paystack dashboard or by bank transfer, "
                           "then record how and mark the status Processed.",
            "fields": ("processed_by", "processed_at", "execution_note", "external_reference"),
        }),
        ("Timestamps", {
            "fields": ("requested_at", "updated_at"),
        }),
    )

    @admin.action(description="Mark processed (money has been returned)")
    def mark_processed(self, request, queryset):
        from django.contrib import messages

        from .refunds import mark_refund_processed

        done = 0
        for refund in queryset.filter(status=Refund.Status.PENDING):
            try:
                mark_refund_processed(
                    refund,
                    by_user=request.user,
                    execution_note=f"Marked processed via admin by {request.user.email}",
                )
                done += 1
            except ValueError as exc:
                self.message_user(request, f"{refund}: {exc}", messages.WARNING)
        self.message_user(request, f"{done} refund(s) marked processed.", messages.SUCCESS)

    @admin.action(description="Reject selected pending refunds")
    def reject_refunds(self, request, queryset):
        from django.contrib import messages

        updated = queryset.filter(status=Refund.Status.PENDING).update(status=Refund.Status.REJECTED)
        self.message_user(request, f"{updated} refund(s) rejected.", messages.SUCCESS)

    @admin.display(description="Execute on gateway")
    def gateway_instruction(self, obj):
        return (obj.breakdown or {}).get("execute_on_gateway", "—")

    @display(description="Status", ordering="status", label=REFUND_STATUS_BADGE)
    def status_badge(self, obj):
        return obj.status, obj.get_status_display()

    def has_add_permission(self, request):
        # Refunds are created by the "Cancel + compute refund" booking action,
        # never free-hand — the computed breakdown is the point.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FxRate)
class FxRateAdmin(ModelAdmin):
    list_display = ["base", "quote", "rate", "source", "fetched_at"]
    list_filter = ["base", "quote", "source"]
    ordering = ["-fetched_at"]
    readonly_fields = ["id", "base", "quote", "rate", "source", "fetched_at"]

    def has_add_permission(self, request):
        return False  # rows are appended by the FX service only

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OpsConfig)
class OpsConfigAdmin(ModelAdmin):
    """Single-row operational settings. Secrets stay in the environment."""

    fields = ["alert_email", "updated_at"]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        return not OpsConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScheduledTask)
class ScheduledTaskAdmin(ModelAdmin):
    list_display = ["name", "is_enabled", "last_run_at", "last_result_short"]
    list_editable = ["is_enabled"]
    readonly_fields = ["name", "last_run_at", "last_result"]
    fields = ["name", "is_enabled", "last_run_at", "last_result"]

    @admin.display(description="Last result")
    def last_result_short(self, obj):
        return (obj.last_result or "—")[:80]

    def has_add_permission(self, request):
        return False  # rows are created by the scheduler itself

    def has_delete_permission(self, request, obj=None):
        return False
