from django import forms
from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.contrib.filters.admin import ChoicesDropdownFilter, RelatedDropdownFilter
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminTextareaWidget

IMAGE_PREVIEW_CLASSES = "rounded-default border border-base-200 dark:border-base-800"

from .models import (
    Destination,
    DestinationImage,
    Itinerary,
    PackageFAQ,
    PackageAddon,
    PackageAddonGroup,
    PackageDiscountRule,
    PackageImage,
    PackageOption,
    PackageVideo,
    PriceListVersion,
    RoomUnit,
    StayImage,
    StayType,
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


class PackageAddonGroupInline(TabularInline):
    """Choice groups for core_plus_addons packages (e.g. 'Accra Accommodation'
    single-select, 'Experiences' multi-select)."""
    model = PackageAddonGroup
    extra = 0
    fields = ["name", "selection", "required", "order"]
    ordering = ["order"]


class PackageAddonInline(TabularInline):
    """Optional add-ons for core_plus_addons packages (hotels, experiences).
    Assign each to a group, or leave ungrouped for an independent option.
    'Unit' decides whether the price is × guests or flat per booking."""
    model = PackageAddon
    extra = 0
    tab = True
    verbose_name_plural = "Add-ons & experiences"
    fields = ["name", "code", "group", "description", "price", "unit", "is_default", "refundable",
              "adults_only", "requires_stay", "vehicle_capacity", "order", "is_active"]
    ordering = ["order"]


class PackageDiscountRuleInline(StackedInline):
    """Non-stacking savings: the engine applies only the single highest
    qualifying rule. Group accommodation rules use min travellers; whole-cart
    rules use required add-ons (add them after saving the add-ons above)."""
    model = PackageDiscountRule
    extra = 0
    tab = True
    verbose_name_plural = "Savings rules"
    fields = ["name", "scope", "percent", "min_travellers", "required_addons", "is_active"]
    filter_horizontal = ["required_addons"]


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


class PackageVideoInline(TabularInline):
    model = PackageVideo
    extra = 0
    fields = ["video_preview", "video", "video_url", "poster", "caption", "order"]
    readonly_fields = ["video_preview"]
    ordering = ["order"]

    @admin.display(description="Preview")
    def video_preview(self, obj):
        if obj.pk and obj.video:
            return format_html(
                '<video src="{}" class="max-h-[90px] {}" controls muted preload="metadata"></video>',
                obj.video.url, IMAGE_PREVIEW_CLASSES,
            )
        if obj.pk and obj.video_url:
            return format_html('<a href="{}" target="_blank">external video ↗</a>', obj.video_url)
        return "—"


class PackageFAQInline(StackedInline):
    model = PackageFAQ
    extra = 1
    tab = True
    verbose_name_plural = "FAQs"
    fields = ["topic", "question", "answer", "order"]
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
    actions_detail = ["publish_prices"]
    change_form_before_template = "admin/packages/price_list_panel.html"
    list_display = ["title", "category", "pricing_model", "from_price_display", "price_list_display", "currency", "is_featured", "is_active"]
    list_filter = [("category", ChoicesDropdownFilter), "is_active", "is_featured"]
    list_editable = ["is_featured", "is_active"]
    search_fields = ["title", "destinations__name", "description"]
    prepopulated_fields = {"slug": ("title",)}
    ordering = ["-is_featured", "title"]
    autocomplete_fields = ["destinations"]
    inlines = [PackageOptionInline, TourDepartureInline, PackageAddonGroupInline, PackageAddonInline,
               PackageDiscountRuleInline, PackageImageInline, PackageVideoInline, PackageFAQInline, ItineraryInline]
    fieldsets = (
        (None, {
            "fields": ("title", "slug", "category", "destinations"),
        }),
        ("Content", {
            "classes": ["tab"],
            "fields": ("description", "highlights", "whats_included", "whats_excluded"),
        }),
        ("Day Tour", {
            "classes": ["tab"],
            "description": "Flat per-person single-day tours. Tick 'is day tour', set the GHS "
                           "price under Legacy Tier Pricing (Shared), add the Saturdays under "
                           "Departures below, and optionally a ~USD figure for display.",
            "fields": ("is_day_tour", "price_usd_estimate"),
        }),
        ("Pricing Model", {
            "classes": ["tab"],
            "description": "How this package is priced. 'Core tour + add-ons' uses the mandatory "
                           "base price per person below, plus the Add-on Groups / Add-ons / "
                           "Discount Rules inlines (e.g. the Michael Blackson experience). Leave "
                           "as option-based (hotel options) or flat (day tour / legacy) otherwise.",
            "fields": ("pricing_model", "base_price_per_person"),
            # Component pricing: Core price here, stays/experiences/savings in their
            # tabs; customers see the last PUBLISHED price list (button at the top).
        }),
        ("Dates & Deadlines", {
            "classes": ["tab"],
            "description": "For option-based tours, Available From/To ARE the tour dates. "
                           "The final payment deadline applies live to every booking "
                           "without a per-booking override.",
            "fields": ("duration_days", "max_guests", "min_group_size", "capacity", "available_from", "available_to",
                       "early_bird_deadline", "final_payment_deadline"),
        }),
        ("Payment Terms", {
            "classes": ["tab"],
            "description": "Paystack Ghana charges GHS only. Non-GHS packages convert at charge "
                           "time using the live market rate plus the margin (rate history is in "
                           "Payments → Exchange Rates); the manual rate is the fallback if the "
                           "rate feed is down. The ledger stays in the package currency.",
            "fields": ("allow_installments", "deposit_minimum", "deposit_unit", "accommodation_deposit_percent", "currency",
                       "fx_mode", "fx_margin_percent", "charge_exchange_rate"),
        }),
        ("Visa Add-on", {
            "classes": ["tab"],
            "fields": ("visa_addon_enabled", "visa_fee", "visa_info"),
        }),
        ("Refund Policy", {
            "classes": ["tab"],
            "fields": ("refund_tiers",),
        }),
        ("Legacy Tier Pricing", {
            "classes": ["tab"],
            "description": "Only for packages WITHOUT hotel/occupancy options below. "
                           "Leave empty on option-based tours.",
            "fields": ("price_shared", "price_private", "price_vip"),
        }),
        ("Visibility", {
            "classes": ["tab"],
            "fields": ("is_active", "is_featured"),
        }),
    )

    def get_inlines(self, request, obj):
        """Component packages edit stays, experiences, savings and price lists;
        older models keep their hotel options, departures and add-on groups."""
        if obj and obj.pricing_model == TravelPackage.PricingModel.COMPONENT:
            return [StayTypeInline, PackageAddonInline, PackageDiscountRuleInline, PriceListVersionInline,
                    PackageImageInline, PackageVideoInline, PackageFAQInline, ItineraryInline]
        return super().get_inlines(request, obj)

    @admin.display(description="From")
    def from_price_display(self, obj):
        return f"{obj.currency} {obj.from_price:,.2f}" if obj.from_price else "—"

    @admin.display(description="Price list")
    def price_list_display(self, obj):
        live = obj.price_lists.filter(status=PriceListVersion.Status.LIVE).first()
        return f"v{live.number} live" if live else ("Draft only" if obj.pricing_model == "component" else "—")

    @action(description="Publish price list", url_path="publish-prices", icon="publish")
    def publish_prices(self, request, object_id):
        from django.contrib import messages
        from django.shortcuts import redirect
        from bookings.services import publish_price_list
        package = TravelPackage.objects.get(pk=object_id)
        if package.pricing_model != TravelPackage.PricingModel.COMPONENT:
            self.message_user(request, "Only component-priced packages have versioned price lists.", messages.WARNING)
        else:
            v = publish_price_list(package, user=request.user, note=f"Published by {request.user.email}")
            self.message_user(request, f"Price list v{v.number} is live. Existing bookings keep their version.", messages.SUCCESS)
        return redirect("admin:packages_travelpackage_change", object_id)

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


# ── Component pricing: stays, rooms, price lists (Change Request v2) ────────

class StayTypeInline(TabularInline):
    model = StayType
    extra = 0
    tab = True
    verbose_name_plural = "Stays"
    fields = ["code", "name", "kind", "capacity", "min_occupancy", "price_per_night", "nights",
              "on_sale", "note", "order"]
    show_change_link = True  # photos, bedding, amenities: on the stay's own page


class StayImageInline(ImagePreviewMixin, TabularInline):
    model = StayImage
    extra = 1
    fields = ["image_preview", "image", "alt_text", "order"]
    readonly_fields = ["image_preview"]


@admin.register(StayType)
class StayTypeAdmin(ModelAdmin):
    """What guests see on the Stays page. Prices and capacity are edited on the
    package (Stays tab) and go live with "Publish price list"."""
    list_display = ["name", "package", "kind", "photo_count", "on_sale"]
    list_filter = [("package", RelatedDropdownFilter)]
    readonly_fields = ["package", "code", "kind", "capacity", "price_per_night", "nights", "on_sale"]
    fieldsets = (
        ("Stay", {"fields": ("package", "code", "name", "kind", "capacity", "price_per_night", "nights", "on_sale", "note",
                            "stay_only", "check_in", "check_out_by")}),
        ("Shown on the Stays page", {"fields": ("bedding", "amenities", "breakfast", "location")}),
    )
    inlines = [StayImageInline]

    @admin.display(description="Photos")
    def photo_count(self, obj):
        return obj.images.count()

    def has_add_permission(self, request):
        return False  # stays are added on the package


class PriceListVersionInline(TabularInline):
    model = PriceListVersion
    extra = 0
    tab = True
    verbose_name_plural = "Price list versions"
    fields = ["number", "status", "note", "published_by", "published_at"]
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class RoomStateFilter(admin.SimpleListFilter):
    title = "state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("sold", "Sold"), ("held", "Held"), ("free", "Free"), ("blocked", "Blocked")]

    def queryset(self, request, queryset):
        from django.utils import timezone
        now = timezone.now()
        v = self.value()
        if v == "sold":
            return queryset.filter(booking__isnull=False, held_until__isnull=True)
        if v == "held":
            return queryset.filter(held_until__gt=now)
        if v == "free":
            return RoomUnit.objects.available(now).filter(pk__in=queryset.values("pk"))
        if v == "blocked":
            return queryset.filter(blocked=True)
        return queryset


def room_state(unit):
    from django.utils import timezone
    if unit.blocked and not unit.booking_id:
        return "blocked", "Blocked"
    if unit.held_until and unit.held_until > timezone.now():
        return "held", ("Unpaid booking" if unit.booking_id else "Checkout hold")
    if unit.booking_id and not unit.held_until:
        return "sold", "Sold"
    return "free", "Free"


@admin.register(RoomUnit)
class RoomUnitAdmin(ModelAdmin):
    """Room allocation (canvas: DjRooms). Units are allocated for the whole
    Accra stay (6–7 and 8–11 Jan); the included Crown Forest night is not a unit."""
    list_display = ["label", "stay_name", "capacity", "state_badge", "booking_link", "occupants", "hold_expires",
                    "check_in", "check_out"]
    list_filter = [("stay_type", RelatedDropdownFilter), RoomStateFilter]
    list_filter_submit = True
    search_fields = ["label", "booking__reference", "occupants__full_name"]
    list_select_related = ["stay_type", "booking"]
    actions = ["release_holds", "block_units", "unblock_units", "move_allocation"]
    list_before_template = "admin/packages/room_summary.html"
    fields = ["stay_type", "label", "blocked", "booking", "held_until", "check_in", "check_out"]
    readonly_fields = ["booking", "held_until", "check_in", "check_out"]
    ordering = ["stay_type__package", "stay_type__order", "label"]

    def changelist_view(self, request, extra_context=None):
        from django.utils import timezone
        rows = []
        for st in StayType.objects.select_related("package").order_by("package__title", "kind", "order"):
            states = [room_state(u)[0] for u in st.units.all()]
            rows.append({"stay": st, "total": len(states), **{k: states.count(k) for k in ("sold", "held", "free", "blocked")}})
        return super().changelist_view(request, {**(extra_context or {}), "room_rows": rows, "now": timezone.now()})

    @admin.display(description="Stay type", ordering="stay_type__order")
    def stay_name(self, obj):
        return obj.stay_type.name

    @admin.display(description="Capacity")
    def capacity(self, obj):
        return f"{obj.stay_type.capacity} guest{'s' if obj.stay_type.capacity != 1 else ''}"

    @display(description="State", label={"sold": "success", "held": "warning", "free": "info", "blocked": "danger"})
    def state_badge(self, obj):
        return room_state(obj)

    @admin.display(description="Booking")
    def booking_link(self, obj):
        from django.urls import reverse
        if not obj.booking_id:
            return "—"
        return format_html('<a href="{}" class="az-link">{}</a>',
                           reverse("admin:bookings_booking_change", args=[obj.booking_id]), obj.booking.reference)

    @admin.display(description="Guests")
    def occupants(self, obj):
        return ", ".join(obj.occupants.values_list("full_name", flat=True)) or "—"

    @admin.display(description="Hold expires")
    def hold_expires(self, obj):
        from django.utils import timezone
        if obj.held_until and obj.held_until > timezone.now():
            return obj.held_until.strftime("%d %b %H:%M")
        return "—"

    @admin.action(description="Release checkout holds (not unpaid bookings)")
    def release_holds(self, request, queryset):
        from django.contrib import messages
        n = queryset.filter(booking__isnull=True, held_until__isnull=False).update(hold_token="", held_until=None)
        self.message_user(request, f"{n} hold(s) released.", messages.SUCCESS)

    @admin.action(description="Block (stop selling these units)")
    def block_units(self, request, queryset):
        from django.contrib import messages
        n = queryset.update(blocked=True)
        sold = queryset.filter(booking__isnull=False).count()
        self.message_user(request, f"{n} unit(s) blocked." + (f" {sold} still have a booking — move them first." if sold else ""),
                          messages.WARNING if sold else messages.SUCCESS)

    @admin.action(description="Unblock")
    def unblock_units(self, request, queryset):
        from django.contrib import messages
        self.message_user(request, f"{queryset.update(blocked=False)} unit(s) back on sale.", messages.SUCCESS)

    @admin.action(description="Move allocation to a free unit of the same type")
    def move_allocation(self, request, queryset):
        from django.contrib import messages
        from django.template.response import TemplateResponse
        from bookings.services import IllegalTransition, reassign_unit

        units = list(queryset.filter(booking__isnull=False).select_related("stay_type", "booking"))
        if not units:
            self.message_user(request, "Choose units that have a booking.", messages.WARNING)
            return None
        if "apply" in request.POST:
            for unit in units:
                target = RoomUnit.objects.filter(pk=request.POST.get(f"target_{unit.pk}")).first()
                if not target:
                    continue
                try:
                    reassign_unit(unit, target)
                    self.message_user(request, f"{unit.booking.reference}: {unit.label} → {target.label}.", messages.SUCCESS)
                except IllegalTransition as exc:
                    self.message_user(request, f"{unit.label}: {exc}", messages.ERROR)
            return None
        free = {u.pk: list(RoomUnit.objects.available().filter(stay_type=u.stay_type).exclude(pk=u.pk)) for u in units}
        return TemplateResponse(request, "admin/packages/move_allocation.html", {
            **self.admin_site.each_context(request), "title": "Move allocation", "opts": self.model._meta,
            "rows": [(u, free[u.pk]) for u in units], "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        })
