from django import forms
from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.contrib.filters.admin import ChoicesDropdownFilter, RelatedDropdownFilter
from unfold.widgets import UnfoldAdminTextareaWidget

IMAGE_PREVIEW_CLASSES = "rounded-default border border-base-200 dark:border-base-800"

from .models import (
    Destination,
    DestinationImage,
    Itinerary,
    PackageFAQ,
    PackageImage,
    PackageOption,
    TourDeparture,
    TravelPackage,
    TripUpdate,
)


class LineByLineArrayField(forms.CharField):
    """
    Stores a JSON list but lets the admin user type one item per line
    instead of raw JSON. Works with any JSONField that holds a list.
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", UnfoldAdminTextareaWidget(attrs={"rows": 5}))
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return value or ""

    def to_python(self, value):
        if not value or not value.strip():
            return []
        return [line.strip() for line in value.splitlines() if line.strip()]


class RefundTiersField(forms.CharField):
    """
    Edits the refund_tiers JSON as one "days:percent" pair per line, matching
    the house line-per-item style, e.g.:

        60:90
        30:60
        14:40
        0:0

    means: 60+ days before departure -> 90% refund, 30-59 -> 60%, etc.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", UnfoldAdminTextareaWidget(attrs={"rows": 5}))
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, list):
            return "\n".join(f"{t.get('min_days')}:{t.get('percent')}" for t in value)
        return value or ""

    def to_python(self, value):
        if not value or not value.strip():
            return []
        tiers = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                days, percent = line.split(":")
                tiers.append({"min_days": int(days.strip()), "percent": int(percent.strip())})
            except (ValueError, TypeError):
                raise forms.ValidationError(
                    f"Invalid refund tier '{line}'. Use 'days:percent' per line, e.g. '60:90'."
                )
        tiers.sort(key=lambda t: -t["min_days"])
        for tier in tiers:
            if not (0 <= tier["percent"] <= 100):
                raise forms.ValidationError("Refund percent must be between 0 and 100.")
            if tier["min_days"] < 0:
                raise forms.ValidationError("Refund tier days must be 0 or more.")
        return tiers


class TravelPackageForm(forms.ModelForm):
    highlights = LineByLineArrayField(
        label="Highlights",
        help_text="One highlight per line.",
    )
    whats_included = LineByLineArrayField(
        label="What's Included",
        help_text="One item per line, e.g. 'Beverages and drinking water'.",
    )
    whats_excluded = LineByLineArrayField(
        label="What's Excluded",
        help_text="One item per line, e.g. 'International flights'.",
    )
    refund_tiers = RefundTiersField(
        label="Refund tiers",
        help_text="One 'days-before-departure:refund-percent' pair per line, "
                  "e.g. '60:90' = 90% refund when cancelling 60+ days out. "
                  "Applied to NEW bookings only — existing bookings keep the "
                  "tiers they were created with.",
    )

    class Meta:
        model = TravelPackage
        fields = "__all__"


class ItineraryInlineForm(forms.ModelForm):
    activities = LineByLineArrayField(
        label="Activities",
        help_text="One activity per line.",
    )

    class Meta:
        model = Itinerary
        fields = "__all__"


class PackageOptionInline(TabularInline):
    model = PackageOption
    extra = 0
    fields = ["hotel_name", "star_rating", "occupancy", "price_per_person",
              "early_bird_price_per_person", "hotel_image", "is_active", "order"]
    ordering = ["order"]


class TourDepartureInline(TabularInline):
    """Scheduled dates a day tour runs — add one row per Saturday."""
    model = TourDeparture
    extra = 0
    fields = ["date", "capacity", "seats_taken", "is_active", "note"]
    readonly_fields = ["seats_taken"]
    ordering = ["date"]


class ImagePreviewMixin:
    """Read-only thumbnail column for image inlines (same look as blog's)."""

    @admin.display(description="Preview")
    def image_preview(self, obj):
        if obj.pk and obj.image:
            return format_html(
                '<img src="{}" class="max-h-[90px] {}" />',
                obj.image.url,
                IMAGE_PREVIEW_CLASSES,
            )
        return "—"


class PackageImageInline(ImagePreviewMixin, TabularInline):
    model = PackageImage
    extra = 1
    fields = ["image_preview", "image", "caption", "is_cover", "order"]
    readonly_fields = ["image_preview"]
    ordering = ["order"]


class DestinationImageInline(ImagePreviewMixin, TabularInline):
    model = DestinationImage
    extra = 1
    fields = ["image_preview", "image", "caption", "is_cover", "order"]
    readonly_fields = ["image_preview"]
    ordering = ["order"]


class PackageFAQInline(StackedInline):
    model = PackageFAQ
    extra = 1
    fields = ["question", "answer", "order"]
    ordering = ["order"]


class ItineraryInline(StackedInline):
    model = Itinerary
    form = ItineraryInlineForm
    extra = 0
    fields = ["day", "title", "description", "activities", "notes"]
    ordering = ["day"]


@admin.register(TravelPackage)
class TravelPackageAdmin(ModelAdmin):
    form = TravelPackageForm
    actions = ["send_newsletter_announcement"]
    list_display = ["title", "category", "destinations_display", "duration_days", "price_shared", "price_private", "price_vip", "currency", "is_featured", "is_active"]
    list_filter = [("category", ChoicesDropdownFilter), "is_active", "is_featured"]
    list_editable = ["is_featured", "is_active"]
    search_fields = ["title", "destinations__name", "description"]
    prepopulated_fields = {"slug": ("title",)}
    ordering = ["-is_featured", "title"]
    autocomplete_fields = ["destinations"]
    inlines = [PackageOptionInline, TourDepartureInline, PackageImageInline, PackageFAQInline, ItineraryInline]
    fieldsets = (
        (None, {
            "fields": ("title", "slug", "category", "destinations"),
        }),
        ("Content", {
            "fields": ("description", "highlights", "whats_included", "whats_excluded"),
        }),
        ("Day Tour", {
            "description": "Flat per-person single-day tours. Tick 'is day tour', set the GHS "
                           "price under Legacy Tier Pricing (Shared), add the Saturdays under "
                           "Departures below, and optionally a ~USD figure for display.",
            "fields": ("is_day_tour", "price_usd_estimate"),
        }),
        ("Dates & Deadlines", {
            "description": "For option-based tours, Available From/To ARE the tour dates. "
                           "The final payment deadline applies live to every booking "
                           "without a per-booking override.",
            "fields": ("duration_days", "max_guests", "available_from", "available_to",
                       "early_bird_deadline", "final_payment_deadline"),
        }),
        ("Payment Terms", {
            "description": "Paystack Ghana charges GHS only. Non-GHS packages convert at charge "
                           "time using the live market rate plus the margin (rate history is in "
                           "Payments → Exchange Rates); the manual rate is the fallback if the "
                           "rate feed is down. The ledger stays in the package currency.",
            "fields": ("allow_installments", "deposit_minimum", "currency",
                       "fx_mode", "fx_margin_percent", "charge_exchange_rate"),
        }),
        ("Visa Add-on", {
            "fields": ("visa_addon_enabled", "visa_fee", "visa_info"),
        }),
        ("Refund Policy", {
            "fields": ("refund_tiers",),
        }),
        ("Legacy Tier Pricing", {
            "classes": ("collapse",),
            "description": "Only for packages WITHOUT hotel/occupancy options below. "
                           "Leave empty on option-based tours.",
            "fields": ("price_shared", "price_private", "price_vip"),
        }),
        ("Visibility", {
            "fields": ("is_active", "is_featured"),
        }),
    )

    @admin.display(description="Destinations")
    def destinations_display(self, obj):
        return ", ".join(destination.name for destination in obj.destinations.all()) or "-"

    @admin.action(description="Send newsletter announcement to subscribers")
    def send_newsletter_announcement(self, request, queryset):
        """Deliberate replacement for the old post_save auto-announcement:
        announcements only go out when an admin explicitly triggers them,
        and only for active packages."""
        from django.contrib import messages
        from newsletter.services import send_new_package_announcement

        sent, skipped = 0, 0
        for package in queryset:
            if not package.is_active:
                skipped += 1
                continue
            send_new_package_announcement(package_id=package.id)
            sent += 1

        if sent:
            self.message_user(request, f"Announcement sent for {sent} package(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} inactive package(s) — activate them first.",
                messages.WARNING,
            )


@admin.register(Destination)
class DestinationAdmin(ModelAdmin):
    list_display = ["name", "latitude", "longitude", "created_at", "updated_at"]
    search_fields = ["name", "description"]
    ordering = ["name"]
    inlines = [DestinationImageInline]
    fieldsets = (
        (None, {
            "fields": ("name", "description"),
        }),
        ("Location", {
            "fields": ("map_url", "latitude", "longitude"),
        }),
    )


@admin.register(TripUpdate)
class TripUpdateAdmin(ModelAdmin):
    list_display = ["title", "package", "is_published", "published_at", "created_at"]
    list_filter = ["is_published", ("package", RelatedDropdownFilter)]
    search_fields = ["title", "body"]
    fields = ["package", "title", "body", "is_published", "published_at"]
    readonly_fields = ["published_at"]
