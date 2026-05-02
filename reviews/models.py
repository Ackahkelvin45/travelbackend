from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
import uuid


class Review(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        "packages.TravelPackage",
        on_delete=models.CASCADE,
        related_name="reviews",
    )

    # Optional — registered users attach their account for profile history.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews",
    )

    # Always captured — works for both guests and registered users.
    reviewer_name = models.CharField(max_length=200)
    reviewer_email = models.EmailField()

    # Linking to a booking lets us mark the review as verified.
    # Guests can do this too via booking reference.
    booking = models.OneToOneField(
        "bookings.Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review",
    )

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    title = models.CharField(max_length=200, blank=True, null=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reviewer_name} — {self.rating}★ on {self.package.title}"
