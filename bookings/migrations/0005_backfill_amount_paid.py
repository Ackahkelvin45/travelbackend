"""
Backfill Booking.amount_paid from the existing payment ledger.

Before the ledger migration each booking had at most one payment (OneToOne),
so amount_paid is simply the amount of its successful payment, if any.
"""

from django.db import migrations
from django.db.models import Sum


def backfill_amount_paid(apps, schema_editor):
    Booking = apps.get_model("bookings", "Booking")
    Payment = apps.get_model("payments", "Payment")

    paid = (
        Payment.objects.filter(status="success")
        .values("booking_id")
        .annotate(total=Sum("amount"))
    )
    for row in paid:
        Booking.objects.filter(pk=row["booking_id"]).update(amount_paid=row["total"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0004_booking_amount_paid_booking_amount_refunded"),
        ("payments", "0002_payment_method_payment_needs_review_payment_note_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_amount_paid, noop),
    ]
