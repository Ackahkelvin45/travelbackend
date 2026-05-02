from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Booking


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    list_display = ["reference", "full_name", "email", "package", "num_guests", "travel_date", "total_amount", "currency", "status", "created_at"]
    list_filter = ["status", "currency", "travel_date"]
    search_fields = ["reference", "email", "first_name", "last_name", "phone"]
    readonly_fields = ["reference", "unit_price", "total_amount", "created_at", "updated_at"]
    ordering = ["-created_at"]
    fieldsets = (
        ("Booking Reference", {
            "fields": ("reference", "status"),
        }),
        ("Guest Info", {
            "fields": ("user", "first_name", "last_name", "email", "phone", "country"),
        }),
        ("Trip Details", {
            "fields": ("package", "num_guests", "travel_date", "special_requests"),
        }),
        ("Financials", {
            "fields": ("unit_price", "total_amount", "currency"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )
