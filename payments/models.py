from django.db import models
import uuid


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        ABANDONED = "abandoned", "Abandoned"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Links to Booking — NOT User — so guest payments work without an account.
    booking = models.OneToOneField(
        "bookings.Booking",
        on_delete=models.PROTECT,
        related_name="payment",
    )

    # paystack_reference       → unique tx ref sent to Paystack
    # paystack_access_code     → returned by initialize, opens Paystack popup
    # paystack_authorization_url → redirect URL for standard checkout
    paystack_reference = models.CharField(max_length=100, unique=True)
    paystack_access_code = models.CharField(max_length=100, blank=True, null=True)
    paystack_authorization_url = models.URLField(blank=True, null=True)

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount in major currency unit, e.g. 500.00 GHS",
    )
    currency = models.CharField(max_length=3, default="GHS")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # Full Paystack webhook/verify response stored for auditing and disputes.
    gateway_response = models.JSONField(default=dict, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment {self.paystack_reference} [{self.status}]"
