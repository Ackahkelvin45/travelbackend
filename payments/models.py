from django.db import models
import uuid


class Payment(models.Model):
    """
    One row per payment attempt — the payments table is an append-only ledger.

    A booking may have many payments (full payment, deposit, installment
    top-ups, retries after a declined card). Booking-level totals
    (``Booking.amount_paid``) are a cached projection of this ledger,
    written only by ``payments.services.apply_successful_payment`` under a
    row lock on the booking.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        ABANDONED = "abandoned", "Abandoned"
        REFUNDED = "refunded", "Refunded"

    class Purpose(models.TextChoices):
        FULL = "full", "Full payment"
        DEPOSIT = "deposit", "Deposit"
        INSTALLMENT = "installment", "Installment / top-up"

    class Method(models.TextChoices):
        PAYSTACK = "paystack", "Paystack"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Links to Booking — NOT User — so guest payments work without an account.
    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.PROTECT,
        related_name="payments",
    )

    purpose = models.CharField(
        max_length=20,
        choices=Purpose.choices,
        default=Purpose.FULL,
    )
    method = models.CharField(
        max_length=20,
        choices=Method.choices,
        default=Method.PAYSTACK,
    )

    # paystack_reference       → unique tx ref sent to Paystack (fresh per attempt;
    #                            Paystack references are single-use once charged)
    # paystack_access_code     → returned by initialize, opens Paystack popup
    # paystack_authorization_url → redirect URL for standard checkout
    # All nullable: offline payments (bank transfer) have no gateway fields.
    paystack_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    paystack_access_code = models.CharField(max_length=100, blank=True, null=True)
    paystack_authorization_url = models.URLField(blank=True, null=True)

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Ledger amount in the BOOKING currency, e.g. 1000.00 USD",
    )
    currency = models.CharField(max_length=3, default="GHS")

    # Gateway charge, when it differs from the ledger currency: Paystack Ghana
    # charges GHS only, so a USD-denominated booking is charged
    # charged_amount = amount × exchange_rate. The ledger (amount/currency)
    # stays in the booking currency — installment sums always reconcile.
    charged_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Amount actually sent to the gateway (GHS). Null when it equals `amount`.",
    )
    charged_currency = models.CharField(max_length=3, null=True, blank=True)
    exchange_rate = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text="Rate used for this charge (charged_currency per 1 unit of currency).",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # Set when money arrived but could not be applied normally (amount/currency
    # mismatch, payment against a cancelled booking, overpayment). Surfaced in
    # admin for manual follow-up — money is never silently dropped or credited.
    needs_review = models.BooleanField(default=False)
    review_note = models.TextField(blank=True, null=True)

    # Free-text note for offline payments (who recorded it, bank ref, etc.)
    note = models.TextField(blank=True, null=True)

    # Full Paystack webhook/verify response stored for auditing and disputes.
    gateway_response = models.JSONField(default=dict, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        ref = self.paystack_reference or f"offline:{str(self.id)[:8]}"
        return f"Payment {ref} [{self.status}]"

    def save(self, *args, **kwargs):
        # Append-only ledger guard: a payment's financial identity must never
        # change after a terminal status. Metadata (needs_review, notes,
        # gateway_response) stays editable.
        if not self._state.adding:
            original = Payment.objects.filter(pk=self.pk).values(
                "amount", "currency", "booking_id", "status"
            ).first()
            if original and original["status"] == self.Status.SUCCESS:
                if (
                    original["amount"] != self.amount
                    or original["currency"] != self.currency
                    or original["booking_id"] != self.booking_id
                ):
                    raise ValueError(
                        "Successful payments are append-only: amount, currency "
                        "and booking cannot be modified."
                    )
        super().save(*args, **kwargs)


class Refund(models.Model):
    """
    One refund leg against one payment. A policy refund ("60% of what was
    paid") is decomposed by payments.refunds into per-payment legs, because
    gateway refunds are per-transaction.

    v1 execution is manual (Paystack dashboard / bank transfer): the row is
    the computed decision + audit record; an operator marks it processed with
    an external reference. Processed rows are append-only.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refunds",
        help_text="The charge this leg refunds. Required before marking processed.",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reason = models.TextField(blank=True, null=True)

    # Full computation trail from payments.refunds.compute_refund:
    # amount_paid, non-refundable components, refundable base, tier applied,
    # percent, prior refunds — the evidence for any dispute.
    breakdown = models.JSONField(default=dict, blank=True)

    processed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_refunds",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    execution_note = models.TextField(
        blank=True, null=True,
        help_text="How the money was returned (e.g. 'Paystack dashboard refund', bank details).",
    )
    external_reference = models.CharField(
        max_length=200, blank=True, null=True,
        help_text="Gateway/bank reference of the executed refund.",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"Refund {self.amount} {self.currency} for {self.booking.reference} [{self.status}]"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = Refund.objects.filter(pk=self.pk).values(
                "amount", "currency", "booking_id", "payment_id", "status"
            ).first()
            if original and original["status"] == self.Status.PROCESSED:
                if (
                    original["amount"] != self.amount
                    or original["currency"] != self.currency
                    or original["booking_id"] != self.booking_id
                    or original["payment_id"] != self.payment_id
                    or self.status != self.Status.PROCESSED
                ):
                    raise ValueError(
                        "Processed refunds are append-only: amount, currency, "
                        "booking, payment and status cannot be modified."
                    )
        super().save(*args, **kwargs)


class FxRate(models.Model):
    """
    Fetched market exchange rates, appended per successful provider fetch.

    Serves two jobs: a durable last-known-good fallback when the provider is
    down (process caches don't survive restarts), and an audit history of the
    market rates behind every charge.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    base = models.CharField(max_length=3)
    quote = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=14, decimal_places=6)
    source = models.CharField(max_length=100, help_text="Provider the rate came from.")
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [models.Index(fields=["base", "quote", "-fetched_at"])]

    def __str__(self):
        return f"1 {self.base} = {self.rate} {self.quote} ({self.fetched_at:%Y-%m-%d %H:%M})"


class OpsConfig(models.Model):
    """
    Singleton for operational settings the business should control without a
    deploy. Secrets (Paystack keys, API keys) deliberately stay in the
    environment — never in the database.
    """

    alert_email = models.EmailField(
        blank=True,
        help_text="Receives critical payment/FX alerts (gateway mismatches, "
                  "overpayments, rate-feed problems). Falls back to the "
                  "ADMIN_ALERT_EMAIL environment variable when empty.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operational settings"
        verbose_name_plural = "Operational settings"

    def __str__(self):
        return "Operational settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config


class ScheduledTask(models.Model):
    """
    In-app scheduler state — replaces host crontab entries. The middleware in
    payments.autotasks claims and runs due tasks in a background thread, at
    most once per interval, safely across multiple workers (row lock claim).
    Admins can pause a task or read when it last ran.
    """

    name = models.CharField(max_length=100, unique=True)
    is_enabled = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
