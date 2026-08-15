"""
Recompute every booking's cached amount_paid / amount_refunded from the
payment ledger and report (or fix) drift.

The caches are only ever written under a booking row lock by
payments.services, so drift indicates a bug — run with --fix to repair,
without it to audit.
"""

from django.core.management.base import BaseCommand
from django.db.models import Sum

from bookings.models import Booking
from payments.models import Payment
from payments.money import quantize


class Command(BaseCommand):
    help = "Audit (and optionally repair) cached booking totals against the payment ledger."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true", help="Repair drifted rows.")

    def handle(self, *args, **options):
        drifted = 0
        for booking in Booking.objects.all().iterator():
            ledger_paid = quantize(
                Payment.objects.filter(booking=booking, status=Payment.Status.SUCCESS)
                .aggregate(total=Sum("amount"))["total"] or 0
            )
            from payments.models import Refund
            ledger_refunded = quantize(
                Refund.objects.filter(booking=booking, status=Refund.Status.PROCESSED)
                .aggregate(total=Sum("amount"))["total"] or 0
            )

            if booking.amount_paid != ledger_paid or booking.amount_refunded != ledger_refunded:
                drifted += 1
                self.stdout.write(
                    f"DRIFT {booking.reference}: cached paid={booking.amount_paid} "
                    f"ledger={ledger_paid}; cached refunded={booking.amount_refunded} "
                    f"ledger={ledger_refunded}"
                )
                if options["fix"]:
                    Booking.objects.filter(pk=booking.pk).update(
                        amount_paid=ledger_paid, amount_refunded=ledger_refunded
                    )

        if drifted == 0:
            self.stdout.write(self.style.SUCCESS("All booking caches match the ledger."))
        else:
            verb = "repaired" if options["fix"] else "found (run with --fix to repair)"
            self.stdout.write(self.style.WARNING(f"{drifted} drifted booking(s) {verb}."))
