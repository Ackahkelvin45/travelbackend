"""
Refresh exchange rates for every non-GHS currency in use, so the durable
FxRate fallback is always warm — a full provider outage then never blocks
checkout (fallback is valid for FX_MAX_STALENESS_HOURS).

Run daily alongside the reminders cron, e.g.
  30 6 * * *  docker compose -f docker-compose.prod.yml exec -T web \\
              python manage.py refresh_fx_rates
"""

from django.core.management.base import BaseCommand

from packages.models import TravelPackage
from payments.fx import fetch_live_rate


class Command(BaseCommand):
    help = "Fetch and store current FX rates for all non-GHS package currencies."

    def handle(self, *args, **options):
        # Include drafts: the rate history must be warm BEFORE a package goes
        # live, so the first real customer is protected by the sanity checks.
        currencies = set(
            TravelPackage.objects.exclude(currency="GHS").values_list("currency", flat=True)
        )
        if not currencies:
            self.stdout.write("No non-GHS packages — nothing to refresh.")
            return

        for base in sorted(currencies):
            resolved = fetch_live_rate(base, "GHS")
            if resolved:
                self.stdout.write(self.style.SUCCESS(
                    f"1 {base} = {resolved.rate} GHS ({resolved.source})"
                ))
            else:
                self.stderr.write(self.style.ERROR(
                    f"FAILED to refresh {base}->GHS from any provider — "
                    "the DB fallback is aging; investigate."
                ))
