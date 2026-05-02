from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.widgets import UnfoldAdminTextareaWidget

from .models import Itinerary, PackageFAQ, PackageImage, TravelPackage


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


class TravelPackageForm(forms.ModelForm):
    highlights = LineByLineArrayField(
        label="Highlights",
        help_text="One highlight per line.",
    )
    whats_included = LineByLineArrayField(
        label="What's Included",
        help_text="One item per line, e.g. 'Beverages and drinking water'.",
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


class PackageImageInline(TabularInline):
    model = PackageImage
    extra = 1
    fields = ["image", "caption", "is_cover", "order"]
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
    list_display = ["title", "category", "destination", "duration_days", "price_shared", "price_private", "price_vip", "currency", "is_featured", "is_active"]
    list_filter = ["category", "is_active", "is_featured"]
    list_editable = ["is_featured", "is_active"]
    search_fields = ["title", "destination", "description"]
    prepopulated_fields = {"slug": ("title",)}
    ordering = ["-is_featured", "title"]
    inlines = [PackageImageInline, PackageFAQInline, ItineraryInline]
    fieldsets = (
        (None, {
            "fields": ("title", "slug", "category", "destination"),
        }),
        ("Content", {
            "fields": ("description", "highlights", "whats_included"),
        }),
        ("Location", {
            "fields": ("map_url", "latitude", "longitude"),
        }),
        ("Logistics", {
            "fields": ("duration_days", "max_guests", "available_from", "available_to"),
        }),
        ("Pricing", {
            "fields": ("price_shared", "price_private", "price_vip", "currency"),
        }),
        ("Visibility", {
            "fields": ("is_active", "is_featured"),
        }),
    )
