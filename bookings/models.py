from django.conf import settings
from django.db import models
import uuid


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"        # created, awaiting payment
        CONFIRMED = "confirmed", "Confirmed"  # payment successful
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"  # trip has taken place

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=20, unique=True, editable=False)

    # Nullable — guests leave this blank.
    # Registered users get booking history.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings",
        help_text="Populated only for registered users. Guests leave this null.",
    )

    # Always captured — even for registered users — so the record is self-contained.
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, null=True)

    package = models.ForeignKey(
        "packages.TravelPackage",
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    num_guests = models.PositiveIntegerField(default=1)
    travel_date = models.DateField()
    special_requests = models.TextField(blank=True, null=True)

    # Locked at booking time so future price changes don't affect existing bookings.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"AZT-{uuid.uuid4().hex[:8].upper()}"
        if not self.unit_price:
            self.unit_price = self.package.price_shared
        if not self.total_amount:
            self.total_amount = self.unit_price * self.num_guests
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Booking {self.reference} — {self.email}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def is_paid(self):
        return hasattr(self, "payment") and self.payment.status == "success"
