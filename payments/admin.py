from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Payment


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = ["paystack_reference", "booking", "amount", "currency", "status", "paid_at", "created_at"]
    list_filter = ["status", "currency"]
    search_fields = ["paystack_reference", "booking__reference", "booking__email"]
    readonly_fields = ["id", "paystack_reference", "paystack_access_code", "paystack_authorization_url", "gateway_response", "paid_at", "created_at", "updated_at"]
    ordering = ["-created_at"]
    fieldsets = (
        ("Payment Reference", {
            "fields": ("id", "paystack_reference", "paystack_access_code", "paystack_authorization_url"),
        }),
        ("Transaction", {
            "fields": ("booking", "amount", "currency", "status"),
        }),
        ("Gateway Response", {
            "fields": ("gateway_response",),
        }),
        ("Timestamps", {
            "fields": ("paid_at", "created_at", "updated_at"),
        }),
    )
