"""
Seed the flat-price GHS day tours (Accra / Akosombo / Cape Coast / Ada / Aburi)
with their scheduled Saturday departures. Idempotent — safe to run repeatedly:
packages are matched by slug and departures by (package, date).

    python manage.py seed_day_tours
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from packages.models import TourDeparture, TravelPackage


def _next_weekday(d: date, weekday: int) -> date:
    """First date on/after d that falls on `weekday` (Mon=0 … Sat=5)."""
    return d + timedelta(days=(weekday - d.weekday()) % 7)


TOURS = [
    {
        "slug": "accra-city-tour",
        "title": "Accra City Tour",
        "category": TravelPackage.Category.CULTURAL,
        "price_ghs": Decimal("750.00"),
        "price_usd": Decimal("65.00"),
        "description": "A full day through Accra's history, art and food — museums, "
                       "monuments, the art district and lunch at a beloved local spot.",
        "highlights": [
            "National Museum of Ghana", "Kwame Nkrumah Mausoleum",
            "Independence Square", "Accra International Art Centre",
            "Accra Art District", "Lunch at Buka restaurant",
        ],
        "whats_included": [
            "National Museum of Ghana (GHS 60)", "Kwame Nkrumah Mausoleum (GHS 100)",
            "Independence Square (free)", "Accra International Art Centre (free)",
            "Accra Art District (GHS 290)", "Lunch at Buka (GHS 150–300)",
        ],
        # Recurring: every other Saturday.
        "recurrence": "biweekly_saturday",
    },
    {
        "slug": "akosombo-trip",
        "title": "Akosombo Trip",
        "category": TravelPackage.Category.CULTURAL,
        "price_ghs": Decimal("870.00"),
        "price_usd": Decimal("75.00"),
        "description": "Hike Shai Hills, relax lakeside, cruise into the sunset and end "
                       "the day around a bonfire at Akosombo.",
        "highlights": [
            "Shai Hills hike & safari", "Lake club relaxation",
            "Sunset boat cruise", "Nighttime bonfire",
        ],
        "whats_included": [
            "Hike & Safari at Shai Hills (GHS 220)", "Relaxation at lake club",
            "Lunch at lake club (GHS 150–250)", "Sunset boat cruise (GHS 300)",
            "Nighttime bonfire (GHS 100)",
        ],
        "dates": [date(2026, 8, 15)],
    },
    {
        "slug": "cape-coast-tour",
        "title": "Cape Coast Tour",
        "category": TravelPackage.Category.CULTURAL,
        "price_ghs": Decimal("680.00"),
        "price_usd": Decimal("60.00"),
        "description": "The Kakum canopy walk, the historic Cape Coast Castle and a beach "
                       "resort lunch on the coast.",
        "highlights": [
            "Breakfast buffet at Royal Ridge Hotel", "Kakum Canopy Walk",
            "Cape Coast Castle", "Lunch at The Lemon Beach Resort",
        ],
        "whats_included": [
            "Breakfast buffet at Royal Ridge Hotel (GHS 200)",
            "Kakum National Park Canopy Walk (GHS 100)",
            "Cape Coast Castle (GHS 80)", "Lunch at The Lemon Beach Resort (GHS 150–300)",
        ],
        "dates": [date(2026, 9, 19)],
    },
    {
        "slug": "ada-tour",
        "title": "Ada Tour",
        "category": TravelPackage.Category.CULINARY,
        "price_ghs": Decimal("600.00"),
        "price_usd": Decimal("50.00"),
        "description": "A full day at Aqua Safari — food and activities on the Volta estuary.",
        "highlights": ["Full day at Aqua Safari", "Food & activities included"],
        "whats_included": ["Full day at Aqua Safari: food and activities (GHS 600)"],
        "dates": [date(2026, 10, 17)],
    },
    {
        "slug": "aburi-tour",
        "title": "Aburi Tour",
        "category": TravelPackage.Category.CULTURAL,
        "price_ghs": Decimal("650.00"),
        "price_usd": Decimal("55.00"),
        "description": "Waterfalls, the Aburi Botanical Gardens, paintball and a valley-resort "
                       "lunch and swim in the hills above Accra.",
        "highlights": [
            "Oboadaka Waterfalls", "Aburi Botanical Gardens",
            "Paintball in Aburi", "Lunch & swimming at Peduase Valley Resort",
        ],
        "whats_included": [
            "Oboadaka Waterfalls (GHS 100)", "Aburi Botanical Gardens (GHS 50)",
            "Paintball in Aburi", "Lunch & swimming at Peduase Valley Resort (GHS 500)",
        ],
        "dates": [date(2026, 11, 14)],
    },
]


class Command(BaseCommand):
    help = "Create/update the GHS day tours and their Saturday departures (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        today = timezone.now().date()
        created_pkgs = updated_pkgs = dep_count = 0

        for spec in TOURS:
            pkg, created = TravelPackage.objects.update_or_create(
                slug=spec["slug"],
                defaults=dict(
                    title=spec["title"],
                    category=spec["category"],
                    description=spec["description"],
                    highlights=spec["highlights"],
                    whats_included=spec["whats_included"],
                    duration_days=1,
                    # Priced and charged in USD; Paystack settles the GHS
                    # equivalent at the live rate at payment time (same as the
                    # flagship tour). The GHS figures in whats_included are the
                    # informational local cost of each activity.
                    currency="USD",
                    price_shared=spec["price_usd"],
                    price_usd_estimate=None,
                    is_day_tour=True,
                    is_active=True,
                ),
            )
            created_pkgs += created
            updated_pkgs += (not created)

            # Departures
            if spec.get("recurrence") == "biweekly_saturday":
                first = _next_weekday(today, 5)  # next Saturday
                dates = [first + timedelta(weeks=2 * i) for i in range(8)]  # 8 alternating Saturdays
            else:
                dates = spec.get("dates", [])

            for d in dates:
                _, dep_created = TourDeparture.objects.get_or_create(
                    package=pkg, date=d, defaults={"is_active": True}
                )
                dep_count += dep_created

        self.stdout.write(self.style.SUCCESS(
            f"Day tours seeded: {created_pkgs} created, {updated_pkgs} updated, "
            f"{dep_count} new departures added."
        ))
