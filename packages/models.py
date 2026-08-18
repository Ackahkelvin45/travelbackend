from decimal import Decimal

from django.db import models
import uuid


class Destination(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField()
    map_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Google Maps or any map embed/share URL",
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text="Decimal latitude, e.g. 5.603717",
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        help_text="Decimal longitude, e.g. -0.186964",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DestinationImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    destination = models.ForeignKey(
        Destination,
        on_delete=models.CASCADE,
        related_name="images",
    )
    image = models.ImageField(upload_to="destinations/images/")
    caption = models.CharField(max_length=200, blank=True, null=True)
    is_cover = models.BooleanField(default=False, help_text="Mark as the hero/cover image")
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "uploaded_at"]

    def __str__(self):
        return f"Image for {self.destination.name} ({'cover' if self.is_cover else f'order {self.order}'})"

    def save(self, *args, **kwargs):
        # Ensure only one cover image per destination
        if self.is_cover:
            DestinationImage.objects.filter(destination=self.destination, is_cover=True).exclude(pk=self.pk).update(is_cover=False)
        super().save(*args, **kwargs)


class TravelPackage(models.Model):
    class Category(models.TextChoices):
        LUXURY_TRAVEL = "luxury_travel", "Luxury Travel"
        NIGHTLIFE = "nightlife", "Nightlife & Entertainment"
        CULTURAL = "cultural", "Cultural Immersion"
        CULINARY = "culinary", "Culinary Experiences"
        FASHION = "fashion", "Fashion & Lifestyle"
        CORPORATE = "corporate", "Corporate & Group"
        BESPOKE = "bespoke", "Bespoke Concierge"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=30, choices=Category.choices)
    description = models.TextField()
    highlights = models.JSONField(default=list, help_text="List of key highlights")
    whats_included = models.JSONField(
        default=list,
        blank=True,
        help_text="What is included in the package, e.g. ['Beverages', 'Lunch', 'Hotel pickup']",
    )
    destinations = models.ManyToManyField(
        Destination,
        related_name="packages",
        blank=True,
    )
    duration_days = models.PositiveIntegerField()
    max_guests = models.PositiveIntegerField(default=10)

    # ── Pricing ───────────────────────────────────────────────────────────────
    # Each option has a min price; max is optional (null = exact / "from" price).
    # Shared is required — it is the entry-level / best-value option.
    # Nullable: packages that sell through PackageOptions (hotel × occupancy)
    # don't use the flat tier columns at all.
    price_shared = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Shared / Couples price (Best Value). Leave empty for option-based packages.",
    )
    price_private = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Private (Solo) price",
    )
    price_vip = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="VIP price",
    )

    whats_excluded = models.JSONField(
        default=list,
        blank=True,
        help_text="What is NOT included, e.g. ['International flights', 'Travel insurance']",
    )

    currency = models.CharField(max_length=3, default="GHS")

    # For option-based packages these are the TOUR DATES (start / end) —
    # the single place event dates live.
    available_from = models.DateField(null=True, blank=True, help_text="Tour start date (first bookable date for legacy packages)")
    available_to = models.DateField(null=True, blank=True, help_text="Tour end date (last bookable date for legacy packages)")

    # ── Commercial terms for option-based packages ────────────────────────────
    early_bird_deadline = models.DateTimeField(
        null=True, blank=True,
        help_text="Early-bird option prices apply to bookings created before this moment.",
    )
    allow_installments = models.BooleanField(
        default=False,
        help_text="Allow customers to secure a booking with a deposit and pay the balance later.",
    )
    deposit_minimum = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Minimum deposit (in package currency) that confirms an installment booking.",
    )
    final_payment_deadline = models.DateField(
        null=True, blank=True,
        help_text="Installment balances must be fully paid by this date. "
                  "Editing this applies to ALL bookings without a per-booking override.",
    )

    # ── Charging (Paystack Ghana settles GHS only) ────────────────────────────
    # Non-GHS packages (e.g. USD) convert every gateway charge to GHS at
    # PAYMENT time. The booking ledger stays in the package currency — only
    # the charge converts, and the applied rate is snapshotted per payment.
    class FxMode(models.TextChoices):
        LIVE = "live", "Live market rate (+ margin)"
        MANUAL = "manual", "Manual rate only"

    fx_mode = models.CharField(
        max_length=10,
        choices=FxMode.choices,
        default=FxMode.LIVE,
        help_text="Live: fetch the current market rate automatically (manual rate is the "
                  "emergency fallback). Manual: always use the rate below.",
    )
    fx_margin_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("1.50"),
        help_text="Percent added on top of the live market rate to absorb volatility "
                  "between charge and settlement. Defaults to 1.50. Ignored in manual mode.",
    )
    charge_exchange_rate = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text="GHS per 1 unit of the package currency (e.g. 15.5000 for USD→GHS). "
                  "In live mode this is only the fallback when the rate feed is down.",
    )

    # ── Visa add-on (non-refundable third-party cost) ─────────────────────────
    visa_addon_enabled = models.BooleanField(default=False)
    visa_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Visa-on-arrival fee per guest, in package currency.",
    )
    visa_info = models.TextField(
        blank=True, null=True,
        help_text="Guidance shown to guests about the visa process.",
    )

    # ── Refund policy tiers ───────────────────────────────────────────────────
    # Ordered list of {"min_days": int, "percent": int}: the first tier whose
    # min_days <= days-before-departure applies... tiers are matched from most
    # to least days. Snapshotted onto each booking at creation — editing this
    # never changes existing customers' contractual terms.
    refund_tiers = models.JSONField(
        default=list, blank=True,
        help_text='Refund tiers, e.g. [{"min_days": 60, "percent": 90}, '
                  '{"min_days": 30, "percent": 60}, {"min_days": 14, "percent": 40}, '
                  '{"min_days": 0, "percent": 0}]',
    )
    # ── Day tours ─────────────────────────────────────────────────────────────
    # Flat per-person, single-day experiences that run on scheduled Saturdays
    # (see TourDeparture). They reuse the legacy flat-price booking flow; the
    # chosen departure date becomes the booking's travel_date.
    is_day_tour = models.BooleanField(
        default=False,
        help_text="Flat per-person single-day tour with scheduled departure dates.",
    )
    price_usd_estimate = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Optional ~USD figure shown alongside the GHS price (display only, "
                  "never charged). e.g. 65 to show 'GHS 750 / ~$65'.",
    )

    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_featured", "-created_at"]

    def __str__(self):
        return self.title

    def upcoming_departures(self):
        """Active, not-yet-past departures with seats left, soonest first."""
        from django.utils import timezone
        return [
            d for d in self.departures.filter(is_active=True, date__gte=timezone.now().date()).order_by("date")
            if not d.is_full
        ]

    @property
    def has_options(self):
        """Option-based packages use the new hotel/occupancy booking flow."""
        return self.options.filter(is_active=True).exists()

    @property
    def early_bird_active(self):
        from django.utils import timezone
        return bool(self.early_bird_deadline and timezone.now() <= self.early_bird_deadline)

    @property
    def from_price(self):
        """Cheapest current entry price — option-based or legacy tier."""
        prices = [
            (o.early_bird_price_per_person if self.early_bird_active and o.early_bird_price_per_person else o.price_per_person)
            for o in self.options.all() if o.is_active
        ]
        if prices:
            return min(prices)
        return self.price_shared


class PackageOption(models.Model):
    """
    A bookable variant of an option-based package: hotel × occupancy with its
    own standard and early-bird per-person prices. Edited inline on the
    package admin page — for the flagship tour this is 4 rows
    (Four Points/Marriott × single/double).
    """

    class Occupancy(models.TextChoices):
        SINGLE = "single", "Single Occupancy"
        DOUBLE = "double", "Couple / Double Occupancy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="options",
    )
    hotel_name = models.CharField(max_length=200, help_text="e.g. 'Four Points by Sheraton'")
    star_rating = models.PositiveSmallIntegerField(default=4, help_text="Hotel star rating, e.g. 4 or 5")
    hotel_image = models.ImageField(upload_to="packages/hotels/", null=True, blank=True)
    occupancy = models.CharField(max_length=10, choices=Occupancy.choices)
    price_per_person = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Standard price per person, in the package currency.",
    )
    early_bird_price_per_person = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Discounted per-person price while the package's early-bird deadline has not passed.",
    )
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "hotel_name", "occupancy"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "hotel_name", "occupancy"],
                name="unique_option_per_hotel_occupancy",
            ),
        ]

    def __str__(self):
        return f"{self.hotel_name} ({self.star_rating}★) — {self.get_occupancy_display()}"

    @property
    def guests_per_booking(self):
        return 2 if self.occupancy == self.Occupancy.DOUBLE else 1

    def effective_price(self, at=None):
        """(per-person price, early_bird_applied) at the given moment."""
        from django.utils import timezone
        at = at or timezone.now()
        early_bird_ok = (
            self.early_bird_price_per_person is not None
            and self.package.early_bird_deadline is not None
            and at <= self.package.early_bird_deadline
        )
        if early_bird_ok:
            return self.early_bird_price_per_person, True
        return self.price_per_person, False


class TourDeparture(models.Model):
    """
    A scheduled date a day tour runs. A package has many — this is how
    'every other Saturday' and one-off Saturdays are expressed: the operator
    adds each bookable date (optionally with a seat cap). The chosen departure
    becomes the booking's travel_date; `seats_taken` is incremented at booking
    time under a row lock so a departure never oversells.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="departures",
    )
    date = models.DateField(help_text="The Saturday (or any day) this tour runs.")
    capacity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Maximum guests for this departure. Leave blank for unlimited.",
    )
    seats_taken = models.PositiveIntegerField(default=0, editable=False)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, null=True,
                            help_text="Optional label, e.g. 'Limited spots' or 'Holiday special'.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["package", "date"], name="unique_departure_per_package_date"),
        ]

    def __str__(self):
        return f"{self.package.title} — {self.date:%b %d, %Y}"

    @property
    def seats_left(self):
        if self.capacity is None:
            return None  # unlimited
        return max(self.capacity - self.seats_taken, 0)

    @property
    def is_full(self):
        return self.capacity is not None and self.seats_taken >= self.capacity


class PackageImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="images",
    )
    image = models.ImageField(upload_to="packages/images/")
    caption = models.CharField(max_length=200, blank=True, null=True)
    is_cover = models.BooleanField(default=False, help_text="Mark as the hero/cover image")
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "uploaded_at"]

    def __str__(self):
        return f"Image for {self.package.title} ({'cover' if self.is_cover else f'order {self.order}'})"

    def save(self, *args, **kwargs):
        # Ensure only one cover image per package
        if self.is_cover:
            PackageImage.objects.filter(package=self.package, is_cover=True).exclude(pk=self.pk).update(is_cover=False)
        super().save(*args, **kwargs)


class PackageFAQ(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="faqs",
    )
    question = models.CharField(max_length=300)
    answer = models.TextField()
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"FAQ: {self.question[:60]}"


class Itinerary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="itineraries",
    )
    day = models.PositiveIntegerField(help_text="Day number in the trip, e.g. 1, 2, 3")
    title = models.CharField(max_length=200, help_text="e.g. 'Arrival & Welcome Dinner'")
    description = models.TextField(help_text="Full narrative of the day's experience")
    activities = models.JSONField(
        default=list,
        help_text="Ordered list of activities, e.g. ['Airport pickup', 'Hotel check-in']",
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Optional tips or special instructions for the day",
    )

    class Meta:
        ordering = ["package", "day"]
        unique_together = ("package", "day")

    def __str__(self):
        return f"{self.package.title} — Day {self.day}: {self.title}"


class TripUpdate(models.Model):
    """
    An announcement for booked guests (schedule changes, travel info…),
    surfaced on the customer dashboard. Publishing is explicit — drafts are
    invisible to the API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        TravelPackage,
        on_delete=models.CASCADE,
        related_name="trip_updates",
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]

    def __str__(self):
        return f"{self.package.title}: {self.title}"

    def save(self, *args, **kwargs):
        if self.is_published and self.published_at is None:
            from django.utils import timezone
            self.published_at = timezone.now()
        super().save(*args, **kwargs)
