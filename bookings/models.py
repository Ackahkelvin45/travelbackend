from django.conf import settings
from django.db import models
import uuid


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"        # created, awaiting payment / deposit
        CONFIRMED = "confirmed", "Confirmed"  # full payment or required deposit received
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"  # trip has taken place

    class PaymentPlan(models.TextChoices):
        FULL = "full", "Full Payment"
        INSTALLMENT = "installment", "Installment"

    class CancellationReason(models.TextChoices):
        CUSTOMER = "customer", "Customer requested"
        EXPIRED = "expired", "Pending booking expired unpaid"
        OVERDUE_FORFEIT = "overdue_forfeit", "Payment deadline missed"
        ADMIN = "admin", "Cancelled by admin"

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

    # Option-based bookings (hotel × occupancy flow). Null for legacy
    # tier-based bookings.
    option = models.ForeignKey(
        "packages.PackageOption",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
    )
    payment_plan = models.CharField(
        max_length=20,
        choices=PaymentPlan.choices,
        default=PaymentPlan.FULL,
    )
    num_guests = models.PositiveIntegerField(default=1)
    travel_date = models.DateField()
    special_requests = models.TextField(blank=True, null=True)

    # ── Commercial snapshot ───────────────────────────────────────────────────
    # Locked at booking time so future admin edits to packages/options/prices
    # never affect existing bookings. Receipts, refunds and disputes read the
    # snapshot, not the live catalog.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")

    option_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text="What was bought: hotel name, star rating, occupancy, per-person price.",
    )
    early_bird_applied = models.BooleanField(default=False)
    early_bird_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Total early-bird saving locked in at booking time.",
    )
    addons = models.JSONField(
        default=list, blank=True,
        help_text='Add-on line items, e.g. [{"code": "visa", "name": "Visa on Arrival", '
                  '"unit_price": "150.00", "quantity": 2, "refundable": false}]',
    )
    refund_tiers_snapshot = models.JSONField(
        default=list, blank=True,
        help_text="Refund policy tiers the customer accepted at booking time.",
    )
    deposit_required = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Deposit that confirms this booking (installment plan only).",
    )

    # Live-with-override deadline: the package's final_payment_deadline applies
    # unless support grants this booking its own date.
    payment_deadline_override = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # ── Cached ledger projections ─────────────────────────────────────────────
    # Source of truth is the payments/refunds ledger. These caches are written
    # ONLY by payments.services under a row lock on this booking, and can be
    # rebuilt at any time with the `reconcile_bookings` management command.
    amount_paid = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Sum of successful payments. Cached from the payment ledger.",
    )
    amount_refunded = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Sum of processed refunds. Cached from the refund ledger.",
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(
        max_length=20,
        choices=CancellationReason.choices,
        null=True,
        blank=True,
    )

    # Idempotency markers for scheduled reminder emails, e.g. {"7d": "<iso ts>"}
    reminders_sent = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"AZT-{uuid.uuid4().hex[:8].upper()}"
        # Legacy-tier fallback only. Option-based bookings must be priced by
        # the pricing service — guessing here would silently mischarge.
        if self.option_id is None:
            if not self.unit_price:
                self.unit_price = self.package.price_shared
            if not self.total_amount:
                self.total_amount = self.unit_price * self.num_guests
        elif self.unit_price is None or self.total_amount is None:
            raise ValueError(
                "Option-based bookings must be priced by bookings.pricing — "
                "unit_price/total_amount cannot be inferred."
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Booking {self.reference} — {self.email}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def effective_payment_deadline(self):
        """Per-booking override if support granted one, else the live package deadline."""
        return self.payment_deadline_override or self.package.final_payment_deadline

    @property
    def is_expired(self):
        """
        Lazy expiry — no cron. An option-based booking that never received a
        payment goes stale PENDING_BOOKING_TTL_HOURS after creation: payment
        initialization refuses it and dashboards hide it.
        """
        from datetime import timedelta
        from django.conf import settings
        from django.utils import timezone

        if self.option_id is None or self.status != self.Status.PENDING:
            return False
        if self.amount_paid > 0:
            return False
        ttl = timedelta(hours=getattr(settings, "PENDING_BOOKING_TTL_HOURS", 24))
        return timezone.now() > self.created_at + ttl

    @property
    def is_overdue(self):
        """Derived, never stored: confirmed-but-unpaid past the deadline."""
        from django.utils import timezone
        deadline = self.effective_payment_deadline
        return bool(
            deadline
            and self.status == self.Status.CONFIRMED
            and not self.is_paid
            and timezone.now().date() > deadline
        )

    @property
    def balance(self):
        """Outstanding amount in the booking currency (never negative)."""
        return max(self.total_amount - self.amount_paid + self.amount_refunded, 0)

    @property
    def is_paid(self):
        from payments.money import FULLY_PAID_TOLERANCE
        return self.total_amount > 0 and self.balance <= FULLY_PAID_TOLERANCE


class PolicyDocument(models.Model):
    """
    A versioned legal document (terms, installment policy, refund policy,
    privacy). Published documents are immutable — changing the text means
    publishing a new version — so a recorded acceptance always refers to
    exactly the text the customer saw.
    """

    class Type(models.TextChoices):
        TERMS = "terms", "Booking Terms & Conditions"
        INSTALLMENT = "installment", "Installment Payment Policy"
        REFUND = "refund", "Cancellation & Refund Policy"
        PRIVACY = "privacy", "Privacy Policy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=20, choices=Type.choices)
    version = models.CharField(max_length=20, help_text="e.g. '1.0'")
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_current = models.BooleanField(
        default=False,
        help_text="The version shown to customers and required at checkout.",
    )
    published_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when published. Published documents can no longer be edited.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["type", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["type", "version"], name="unique_policy_version"),
        ]

    def __str__(self):
        current = " (current)" if self.is_current else ""
        return f"{self.get_type_display()} v{self.version}{current}"

    def save(self, *args, **kwargs):
        # Immutability: published text is evidence of what customers accepted.
        if not self._state.adding and self.published_at:
            original = PolicyDocument.objects.filter(pk=self.pk).values(
                "type", "version", "body", "published_at"
            ).first()
            if original and original["published_at"] and (
                original["type"] != self.type
                or original["version"] != self.version
                or original["body"] != self.body
            ):
                raise ValueError(
                    "Published policy documents are immutable — publish a new "
                    "version instead of editing this one."
                )
        # Only one current version per type.
        if self.is_current:
            PolicyDocument.objects.filter(type=self.type, is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)


class PolicyAcceptance(models.Model):
    """
    Proof that a customer accepted a specific policy version for a specific
    booking. Written inside the booking-creation transaction — a frontend
    checkbox alone is never the record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name="policy_acceptances",
    )
    document = models.ForeignKey(
        PolicyDocument,
        on_delete=models.PROTECT,
        related_name="acceptances",
    )
    email = models.EmailField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-accepted_at"]
        constraints = [
            models.UniqueConstraint(fields=["booking", "document"], name="one_acceptance_per_document"),
        ]

    def __str__(self):
        return f"{self.email} accepted {self.document} for {self.booking.reference}"
