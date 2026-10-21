"""
Switch the flagship Ghana Experience to the Change Request v2 catalogue
(component pricing): Core $899 per traveller, Wed 6 – Mon 11 Jan 2027 (four
Accra nights + the included Crown Forest night), stays priced per unit,
experiences per guest.

Anything the client has not confirmed is created switched OFF: the Four
Points couple room and the extras bundle. Old hotel add-ons, Kozo and the
whole-cart discount ladder are deactivated (kept for existing bookings).

Idempotent. Room inventory is created only when a stay type has no units yet.
--content also replaces the marketing copy (overview, inclusions, the six-day
programme and FAQs) with the change request's text — it overwrites admin edits,
so run it once when switching over.

    python manage.py seed_ghana_2027 [--content]
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from packages.models import (
    Itinerary, PackageAddon, PackageAddonGroup, PackageDiscountRule, PackageFAQ, RoomUnit, StayType, TravelPackage,
)

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


DESCRIPTION = (
    "Five nights in Ghana: four in Accra and one at Crown Forest, which is included. "
    "Arrive on Wednesday to join the press event and Jubilee House visit."
)
HIGHLIGHTS = ["Press and Jubilee House", "Cape Coast Castle and the Academy", "Crown Forest night and activities",
              "Aburi, Peduase and Oboadaka Waterfalls", "Bella Afrik access"]
INCLUDED = ["Crown Forest night, activities and breakfast", "Press, Jubilee House and Cape Coast programme",
            "Aburi, Peduase and Oboadaka Waterfalls", "Bella Afrik access (drinks are self-pay)"]
EXCLUDED = ["Accra accommodation, unless added", "ENZO VIP, Waterfall Massage and farewell dinner, unless added",
            "Airbnb meals", "Flights, visas, transfers, gratuities and taxes [TO CONFIRM]"]
# (date label, programme, overnight)
PROGRAMME = [
    ("Wed 6 Jan", "Press and Jubilee House. Arrival briefing and welcome.", "Accra"),
    ("Thu 7 Jan", "Cape Coast Castle, Academy, Lemon Beach and Crown Forest.", "Crown Forest (included)"),
    ("Fri 8 Jan", "Crown Forest activities and breakfast. Return to Accra. ENZO VIP evening.", "Accra"),
    ("Sat 9 Jan", "Oboadaka Waterfalls, Aburi and Peduase. Optional massage.", "Accra"),
    ("Sun 10 Jan", "Leisure day. Optional Le Tandem farewell dinner. Bella Afrik access.", "Accra"),
    ("Mon 11 Jan", "Final checkout and departure.", "None"),
]
FAQS = [
    ("Trip", "What are the trip dates?", "Wednesday 6 to Monday 11 January 2027. That is five nights: four in Accra and one at Crown Forest. Monday is checkout and departure."),
    ("Trip", "What does the $899 Core ticket include?", "Crown Forest accommodation, activities and breakfast, plus the core programme: press and Jubilee House, Cape Coast, Aburi, Peduase, Oboadaka Waterfalls and Bella Afrik access. Drinks at Bella Afrik are self-pay. The farewell dinner is optional."),
    ("Trip", "When should I arrive?", "Arrive by [ARRIVAL TIME] on Wednesday 6 January to attend the press event and Jubilee House. Tuesday arrivals need an extra night, booked separately."),
    ("Stays", "Do I have to book accommodation with you?", "No. You can arrange your own. If you add Airbnb or Four Points, every guest in the room needs a Core ticket."),
    ("Stays", "How is accommodation priced?", "Per unit or room, per night, for four Accra nights. A one bedroom Airbnb sleeps up to 2 and a two bedroom up to 4. The unit is charged once, however many guests share it."),
    ("Stays", "Are meals included at the Airbnb?", "No. Airbnb meals are not included. Breakfast at Crown Forest is included in Core. Four Points breakfast is [TO BE CONFIRMED]."),
    ("Prices", "What discounts are available?", "3% on Core plus Airbnb, 5% on Core plus Four Points, and 5% on a guest's full set of experiences. Each discount applies once per item, and there are no coupon codes."),
    ("Prices", "Can my partner and I choose different experiences?", "Yes. Experiences are chosen per guest. A couple can add one for both or pick separately."),
    ("Payments", "How do deposits and payments work?", "[DEPOSIT AND INSTALMENT RULES TO BE CONFIRMED]. You always see the amount due today and the remaining balance before you pay, and we remind you before the balance is due."),
    ("Payments", "What if my payment fails?", "Nothing is booked until payment is verified, and you won't be charged twice. You can retry straight away from the payment page or your booking link."),
    ("Payments", "What is the cancellation policy?", "See our refund policy. [APPROVED CANCELLATION AND REFUND TERMS]"),
    ("Travel", "Do you need my passport?", "Only if a confirmed supplier or activity requires it. We'll ask through a secure upload, never by email or chat."),
    ("Travel", "Are flights, visas or airport transfers included?", "Not unless stated. Flights, visas, transfers, gratuities and taxes are [TO BE CONFIRMED]."),
]


class Command(BaseCommand):
    help = "Apply the Change Request v2 catalogue to the flagship package (component pricing)."

    def add_arguments(self, parser):
        parser.add_argument("--content", action="store_true",
                            help="Also replace overview, inclusions, programme and FAQs (overwrites admin edits).")

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

        if options["content"]:
            self._content(pkg)

        self.stdout.write(self.style.SUCCESS(
            f"{pkg.title}: component pricing, Core ${pkg.base_price_per_person}, "
            f"{pkg.stay_types.count()} stay types, {len(bundle)} experiences, {retired} old add-ons retired. "
            "Couple room and extras bundle are OFF until the client confirms."
        ))

    def _content(self, pkg):
        pkg.description = DESCRIPTION
        pkg.highlights = HIGHLIGHTS
        pkg.whats_included = INCLUDED
        pkg.whats_excluded = EXCLUDED
        pkg.save(update_fields=["description", "highlights", "whats_included", "whats_excluded", "updated_at"])
        pkg.itineraries.filter(day__gt=len(PROGRAMME)).delete()
        for day, (date_label, text, night) in enumerate(PROGRAMME, start=1):
            Itinerary.objects.update_or_create(
                package=pkg, day=day,
                defaults=dict(title=date_label, description=text, activities=[], notes=f"Overnight: {night}"),
            )
        for code, name, *_ in EXPERIENCES:
            pkg.addons.filter(code=code).update(name=name)
        for code, name, *_ in STAYS:
            pkg.stay_types.filter(code=code).update(name=name)
        pkg.faqs.all().delete()
        PackageFAQ.objects.bulk_create(
            PackageFAQ(package=pkg, topic=topic, question=q, answer=a, order=i) for i, (topic, q, a) in enumerate(FAQS)
        )
        self.stdout.write(f"Content replaced: {len(PROGRAMME)} programme days, {len(FAQS)} FAQs.")
