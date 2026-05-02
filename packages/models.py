from django.db import models
import uuid


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
    destination = models.CharField(max_length=200)
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
    duration_days = models.PositiveIntegerField()
    max_guests = models.PositiveIntegerField(default=10)

    # ── Pricing ───────────────────────────────────────────────────────────────
    # Each option has a min price; max is optional (null = exact / "from" price).
    # Shared is required — it is the entry-level / best-value option.
    price_shared = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Shared / Couples price (Best Value)",
    )
    price_private = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Private (Solo) price",
    )
    price_vip = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="VIP price",
    )

    currency = models.CharField(max_length=3, default="GHS")
    available_from = models.DateField(null=True, blank=True, help_text="First date this package can be booked")
    available_to = models.DateField(null=True, blank=True, help_text="Last date this package can be booked")
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_featured", "price_shared"]

    def __str__(self):
        return self.title


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
