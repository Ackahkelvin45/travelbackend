"""
Seed the Michael Blackson Ghana Experience (core_plus_addons pricing model):
a mandatory Core Tour + optional hotel (single-select) + optional experiences
(multi-select) + the 5-rule non-stacking bundle-discount ladder.

Idempotent. Also self-validates that the configured add-ons + discount rules
reproduce every "approx total" from the developer guide.

    python manage.py seed_michael_blackson
"""
from datetime import date
from decimal import Decimal
from itertools import combinations

from django.core.management.base import BaseCommand
from django.db import transaction

from packages.models import (
    Itinerary,
    PackageAddon,
    PackageAddonGroup,
    PackageDiscountRule,
    TravelPackage,
)

SLUG = "michael-blackson-ghana-experience"

ITINERARY = [
    (1, "Arrival & Welcome", "Arrival in Accra, hotel check-in for guests with accommodation, orientation / leisure."),
    (2, "Accra Cultural Experience", "National Museum, Kwame Nkrumah Mausoleum, Independence Square, Arts Centre, Art District, lunch at Buka/Baobab."),
    (3, "Cape Coast & Crown Forest", "Cape Coast Castle, Michael Blackson Academy, lunch at Lemon Beach Resort, overnight at Crown Forest (Standard Room, included in Core)."),
    (4, "Crown Forest Experience", "Crown Forest safari & activities, leisure, return to Accra."),
    (5, "Michael Blackson / VIP Day", "Meet the Press / tourism engagement, proposed Jubilee House visit. Optional: Kozo/Vine Dinner, ENZO VIP Table with Michael Blackson."),
    (6, "Eastern Region Experience", "Oboadaka Waterfalls, Aburi Botanical Gardens, Peduase Valley Resort, lunch/swimming. Optional: Waterfall Massage."),
    (7, "Beach Day & Farewell Dinner", "Beach / leisure, official Farewell Dinner with Michael Blackson and the team."),
    (8, "Shopping, Checkout & Departure", "Makola Market shopping where flight timing permits, checkout, airport transfers."),
]


class Command(BaseCommand):
    help = "Create/update the Michael Blackson Ghana Experience package (idempotent) and validate its pricing."

    @transaction.atomic
    def handle(self, *args, **options):
        pkg, created = TravelPackage.objects.update_or_create(
            slug=SLUG,
            defaults=dict(
                title="Michael Blackson Ghana Experience",
                category=TravelPackage.Category.LUXURY_TRAVEL,
                description="A personalised Michael Blackson Ghana Experience: a mandatory Core Tour "
                            "(Jan 4–11, 2027) plus optional Accra accommodation and VIP experiences.",
                highlights=["Michael Blackson Academy", "Cape Coast Castle", "Crown Forest safari",
                            "ENZO VIP Table with Michael Blackson", "Farewell Dinner with the team"],
                whats_included=["Core Tour (all listed daytime experiences)",
                                "Crown Forest Standard Room + Safari & Activities (Jan 6)"],
                duration_days=8,
                currency="USD",
                pricing_model=TravelPackage.PricingModel.CORE_PLUS_ADDONS,
                base_price_per_person=Decimal("878.00"),
                allow_installments=True,
                deposit_minimum=Decimal("1000.00"),
                available_from=date(2027, 1, 4),
                available_to=date(2027, 1, 11),
                refund_tiers=[{"min_days": 60, "percent": 90}, {"min_days": 30, "percent": 60},
                              {"min_days": 14, "percent": 40}, {"min_days": 0, "percent": 0}],
                is_active=True,
            ),
        )

        # ── Groups ────────────────────────────────────────────────────────────
        hotels, _ = PackageAddonGroup.objects.update_or_create(
            package=pkg, name="Accra Accommodation",
            defaults=dict(selection=PackageAddonGroup.Selection.SINGLE, required=False, order=1),
        )
        experiences, _ = PackageAddonGroup.objects.update_or_create(
            package=pkg, name="Experiences",
            defaults=dict(selection=PackageAddonGroup.Selection.MULTI, required=False, order=2),
        )

        # ── Add-ons ───────────────────────────────────────────────────────────
        def addon(code, name, price, unit, group, default=False, order=0):
            obj, _ = PackageAddon.objects.update_or_create(
                package=pkg, code=code,
                defaults=dict(name=name, price=Decimal(price), unit=unit, group=group,
                              is_default=default, order=order, is_active=True),
            )
            return obj

        PP = PackageAddon.Unit.PER_PERSON
        FLAT = PackageAddon.Unit.PER_BOOKING
        no_hotel = addon("no_hotel", "No Hotel", "0.00", FLAT, hotels, default=True, order=0)
        mid = addon("hotel_mid", "Mid-Tier Accra Hotel (6 nights)", "2000.00", FLAT, hotels, order=1)
        prem = addon("hotel_premium", "Premium Accra Hotel (6 nights)", "3200.00", FLAT, hotels, order=2)
        kozo = addon("kozo_vine", "Kozo / Vine Dinner", "100.00", PP, experiences, order=1)
        enzo = addon("enzo_vip", "ENZO VIP Table with Michael Blackson", "133.00", PP, experiences, order=2)
        massage = addon("waterfall_massage", "Waterfall Massage", "107.00", PP, experiences, order=3)

        # ── Discount rules (highest qualifying wins; never stack) ─────────────
        def rule(name, percent, required):
            r, _ = PackageDiscountRule.objects.update_or_create(
                package=pkg, name=name, defaults=dict(percent=Decimal(percent), is_active=True))
            r.required_addons.set(required)
            return r

        all_extras = [kozo, enzo, massage]
        rule("Core + all non-hotel extras", "5.00", all_extras)
        rule("Core + Mid-Tier Hotel", "7.00", [mid])
        rule("Core + Mid-Tier Hotel + all extras", "10.00", [mid] + all_extras)
        rule("Core + Premium Hotel", "12.00", [prem])
        rule("Core + Premium Hotel + all extras", "15.00", [prem] + all_extras)

        # ── Itinerary ─────────────────────────────────────────────────────────
        for day, title, desc in ITINERARY:
            Itinerary.objects.update_or_create(
                package=pkg, day=day, defaults=dict(title=title, description=desc, activities=[]))

        self.stdout.write(self.style.SUCCESS(
            f"Michael Blackson package {'created' if created else 'updated'}: "
            f"base ${pkg.base_price_per_person}/pp, {pkg.addons.count()} add-ons, "
            f"{pkg.discount_rules.count()} discount rules."
        ))
        self._validate(pkg, base=Decimal("878.00"), mid=mid, prem=prem, extras=all_extras)

    def _validate(self, pkg, base, mid, prem, extras):
        """Confirm the model config reproduces the guide's example totals (1 guest)."""
        rules = list(pkg.discount_rules.prefetch_related("required_addons"))

        def quote(selected):
            codes = {a.code for a in selected}
            subtotal = base + sum(a.price for a in selected)  # 1 guest; flat + per-person same at n=1
            best = Decimal("0")
            for r in rules:
                if {a.code for a in r.required_addons.all()} <= codes:
                    best = max(best, r.percent)
            return (subtotal * (Decimal("1") - best / 100)).quantize(Decimal("1"))

        cases = {
            "Core only": ([], 878),
            "Core + all extras": (extras, 1157),
            "Core + Mid": ([mid], 2677),
            "Core + Mid + all extras": ([mid] + extras, 2896),
            "Core + Premium": ([prem], 3589),
            "Core + Premium + all extras": ([prem] + extras, 3755),
        }
        ok = True
        for label, (sel, expected) in cases.items():
            got = quote(sel)
            mark = "OK" if abs(got - expected) <= 1 else "MISMATCH"
            if mark != "OK":
                ok = False
            self.stdout.write(f"  {mark}: {label} → ${got} (guide ≈ ${expected})")
        self.stdout.write(self.style.SUCCESS("Pricing rules reproduce the guide totals ✓")
                          if ok else self.style.ERROR("Pricing validation FAILED"))
