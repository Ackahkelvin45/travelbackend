"""
Switch the flagship Ghana Experience to the Change Request v2 catalogue
(component pricing): Core $899 per traveller, Wed 6 – Mon 11 Jan 2027 (four
Accra nights + the included Crown Forest night), stays priced per unit,
experiences per guest.

Anything the client has not confirmed is created switched OFF: the Four
Points couple room and the extras bundle. Old hotel add-ons, Kozo and the
whole-cart discount ladder are deactivated (kept for existing bookings).

Idempotent. Room inventory is created only when a stay type has no units yet.

    python manage.py seed_ghana_2027
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from packages.models import PackageAddon, PackageAddonGroup, PackageDiscountRule, RoomUnit, StayType, TravelPackage

SLUG = "michael-blackson-ghana-experience"

# (code, name, kind, capacity, min_occupancy, $/night, discount %, on_sale, units, note)
# ponytail: unit counts are placeholders until the client confirms inventory; edit in admin.
STAYS = [
    ("airbnb_1bed", "One-bedroom Airbnb", StayType.Kind.AIRBNB, 2, 1, "220.00", "3.00", True, 6, ""),
    ("airbnb_2bed", "Two-bedroom Airbnb", StayType.Kind.AIRBNB, 4, 1, "340.00", "3.00", True, 4, ""),
    ("fourpoints_standard", "Four Points standard room", StayType.Kind.HOTEL, 1, 1, "330.00", "5.00", True, 10, ""),
    ("fourpoints_couple", "Four Points couple room", StayType.Kind.HOTEL, 2, 2, "380.00", "5.00", False, 5,
     "Two-adult rate awaiting confirmation"),
]

EXPERIENCES = [
    ("enzo_vip", "ENZO VIP", "220.00", 1),
    ("waterfall_massage", "Waterfall Massage", "100.00", 2),
    ("farewell_dinner", "Curated Farewell Dinner at Le Tandem", "100.00", 3),
]


class Command(BaseCommand):
    help = "Apply the Change Request v2 catalogue to the flagship package (component pricing)."

    @transaction.atomic
    def handle(self, *args, **options):
        pkg = TravelPackage.objects.filter(slug=SLUG).first()
        if pkg is None:
            self.stderr.write("Run seed_michael_blackson first — the flagship package does not exist.")
            return

        pkg.pricing_model = TravelPackage.PricingModel.COMPONENT
        pkg.base_price_per_person = Decimal("899.00")
        pkg.available_from = date(2027, 1, 6)
        pkg.available_to = date(2027, 1, 11)
        pkg.duration_days = 6
        pkg.save()

        for order, (code, name, kind, cap, min_occ, price, pct, on_sale, units, note) in enumerate(STAYS):
            st, _ = StayType.objects.update_or_create(
                package=pkg, code=code,
                defaults=dict(kind=kind, capacity=cap, min_occupancy=min_occ, price_per_night=Decimal(price),
                              nights=4, discount_percent=Decimal(pct), order=order),
                create_defaults=dict(name=name, kind=kind, capacity=cap, min_occupancy=min_occ,
                                     price_per_night=Decimal(price), nights=4, discount_percent=Decimal(pct),
                                     on_sale=on_sale, note=note, order=order),
            )
            if not st.units.exists():
                RoomUnit.objects.bulk_create(
                    RoomUnit(stay_type=st, label=f"{name} {i + 1}") for i in range(units)
                )

        experiences, _ = PackageAddonGroup.objects.get_or_create(
            package=pkg, name="Experiences", defaults=dict(selection=PackageAddonGroup.Selection.MULTI, order=2),
        )
        bundle = []
        for code, name, price, order in EXPERIENCES:
            addon, _ = PackageAddon.objects.update_or_create(
                package=pkg, code=code,
                defaults=dict(price=Decimal(price), unit=PackageAddon.Unit.PER_PERSON, group=experiences,
                              order=order, is_active=True),
                create_defaults=dict(name=name, price=Decimal(price), unit=PackageAddon.Unit.PER_PERSON,
                                     group=experiences, order=order, is_active=True),
            )
            bundle.append(addon)
        retired = pkg.addons.exclude(code__in=[c for c, *_ in EXPERIENCES]).update(is_active=False)

        pkg.discount_rules.filter(scope=PackageDiscountRule.Scope.CART).update(is_active=False)
        rule, created = PackageDiscountRule.objects.get_or_create(
            package=pkg, scope=PackageDiscountRule.Scope.TRAVELLER_BUNDLE,
            defaults=dict(name="Complete extras bundle", percent=Decimal("5.00"), is_active=False),
        )
        if created:
            rule.required_addons.set(bundle)

        self.stdout.write(self.style.SUCCESS(
            f"{pkg.title}: component pricing, Core ${pkg.base_price_per_person}, "
            f"{pkg.stay_types.count()} stay types, {len(bundle)} experiences, {retired} old add-ons retired. "
            "Couple room and extras bundle are OFF until the client confirms."
        ))
