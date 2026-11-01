"""
Switch the flagship Ghana Experience to the Change Request v4 catalogue
(component pricing, 14 Oct 2026): Core $999 per traveller (client, 27 Oct 2026) paid in full,
Wed 6 – Mon 11 Jan 2027 (Crown Forest night on the 7th included in Core),
Airbnb as a fixed five-night block, Four Points as four paid nights,
experiences per guest, airport transfers per vehicle.

The Four Points couple room is on sale (client, 28 Oct 2026). The 5% / 7%
group discounts (whole price) are ON (client, 26 Oct 2026). Stay-only Airbnbs
are their own stay types with their own units, booked for any nights in
January 2027, with at least one experience (client, 28 Oct 2026). The old
farewell dinner add-on (now included in Core), hotel add-ons, Kozo and the
whole-cart discount ladder are deactivated (kept for existing bookings).

Idempotent. Room inventory is created only when a stay type has no units yet.
A new price list is published whenever the catalogue differs from the live one.
--content also replaces the marketing copy (overview, inclusions, the six-day
programme and FAQs) with the change request's text — it overwrites admin edits,
so run it once when switching over.

    python manage.py seed_ghana_2027 [--content]
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction

from packages.models import (
    Itinerary, PackageAddon, PackageAddonGroup, PackageDiscountRule, PackageFAQ, PackageImage, RoomUnit, StayType,
    TravelPackage,
)

SLUG = "michael-blackson-ghana-experience"
# Starter photos (supplied by the client), attached only while the package has none:
# once staff upload their own in the admin, the seed leaves them alone.
SEED_MEDIA = Path(__file__).resolve().parents[2] / "seed_media"
PHOTOS = [
    ("michael-blackson-academy-students.jpg", "Michael Blackson with students at the Michael Blackson Academy"),
    ("michael-blackson-academy-celebration.jpg", "Michael Blackson cutting the Academy's celebration cake"),
]
# Not in the final change request (v4, 14 Oct 2026): the original option-based
# flagship (its Marriott/premium tiers are to be removed) and the August day
# tours. Switched off, never deleted; re-enable in the admin if Azura confirms
# any as December offerings.
RETIRED_SLUGS = ["ghana-flagship-tour-2027", "accra-city-tour", "akosombo-trip", "cape-coast-tour", "ada-tour", "aburi-tour"]

# Stay only: any nights the guest chooses in January 2027, at least one
# (client, 28 Oct 2026, replacing the fixed 20 to 31 January block).
STAY_ONLY_WINDOW = (date(2027, 1, 1), date(2027, 1, 31))

# (code, name, kind, capacity, min_occupancy, $/night, nights, on_sale, units, note[, stay-only])
# ponytail: unit counts are placeholders until the client confirms inventory; edit in admin.
STAYS = [
    ("airbnb_1bed", "One-bedroom Airbnb", StayType.Kind.AIRBNB, 2, 1, "220.00", 5, True, 6, ""),
    ("airbnb_2bed", "Two-bedroom Airbnb", StayType.Kind.AIRBNB, 4, 1, "340.00", 5, True, 4, ""),
    ("fourpoints_standard", "Four Points standard room", StayType.Kind.HOTEL, 1, 1, "330.00", 4, True, 10,
     "Rate provisional"),
    ("fourpoints_couple", "Four Points couple room", StayType.Kind.HOTEL, 2, 2, "380.00", 4, True, 5, ""),
    ("airbnb_1bed_stay", "One-bedroom Airbnb, stay only", StayType.Kind.AIRBNB, 2, 1, "220.00", 1, True, 6, "", True),
    ("airbnb_2bed_stay", "Two-bedroom Airbnb, stay only", StayType.Kind.AIRBNB, 4, 1, "340.00", 1, True, 4, "", True),
]

# (code, name, $, unit, adults_only, requires_stay, description[, guests per vehicle])
# ponytail: transfers are priced per head (client, 24 Oct 2026) as a
# one-guest "vehicle", which keeps them one-per-guest and stay-only.
EXPERIENCES = [
    ("enzo_vip", "ENZO VIP", "220.00", PackageAddon.Unit.PER_PERSON, True, False,
     "Friday night at ENZO with Michael. Guests 18 and over."),
    ("waterfall_massage", "Waterfall Massage", "100.00", PackageAddon.Unit.PER_PERSON, False, False,
     "Saturday at Boti Falls."),
    ("airport_pickup", "Airport pickup", "100.00", PackageAddon.Unit.PER_VEHICLE, False, True,
     "Arrival transfer, per guest. For guests in Azura accommodation.", 1),
    ("airport_return", "Airport pickup + drop-off", "200.00", PackageAddon.Unit.PER_VEHICLE, False, True,
     "Arrival and departure transfers, per guest. For guests in Azura accommodation.", 1),
]

# (name, percent, min travellers) — approved by the client, 26 Oct 2026.
GROUP_DISCOUNTS = [("Group of 4–9", "5.00", 4), ("Group of 10+", "7.00", 10)]


DESCRIPTION = (
    "Six days in Ghana, Wednesday 6 to Monday 11 January 2027: Cape Coast, the Michael Blackson Academy, "
    "a night at Crown Forest, Boti Falls and an Accra city tour, with the welcome and farewell dinners included. "
    "Michael's planned appearances are the welcome dinner, the Academy visit and the ENZO VIP night."
)
HIGHLIGHTS = ["Welcome dinner with Michael Blackson", "Cape Coast Castle and the Michael Blackson Academy",
              "Crown Forest night and activities", "Boti Falls excursion", "Accra city tour and farewell dinner"]
INCLUDED = ["Welcome dinner and farewell dinner", "Cape Coast Castle, Lemon Beach lunch and the Academy visit",
            "Crown Forest night on 7 January, with breakfast and nature activities",
            "Boti Falls group excursion", "Accra city tour with lunch"]
EXCLUDED = ["Accra accommodation, unless added", "ENZO VIP, Waterfall Massage and airport transfers, unless added",
            "Drinks at Bella Afrik (self-pay)"]
# (date label, programme, overnight)
PROGRAMME = [
    ("Wed 6 Jan", "Arrivals, check-in and settling in. Welcome dinner with Michael Blackson. "
                  "A Jubilee House visit is possible but not confirmed.", "Accra"),
    ("Thu 7 Jan", "Cape Coast Castle, lunch at Lemon Beach and a visit to the Michael Blackson Academy with Michael. "
                  "Transfer to Crown Forest.", "Crown Forest (included in Core)"),
    ("Fri 8 Jan", "Breakfast at Crown Forest, wildlife and nature activities. Return to Accra. "
                  "Optional ENZO VIP night with Michael.", "Accra"),
    ("Sat 9 Jan", "Boti Falls group excursion. Optional waterfall massage. "
                  "You may opt out of the excursion; Core is not refunded.", "Accra"),
    ("Sun 10 Jan", "Accra city tour: National Museum, Kwame Nkrumah Mausoleum and Arts Centre, with lunch. "
                   "Farewell dinner, included in Core.", "Accra"),
    ("Mon 11 Jan", "Checkout and departure. Optional airport transfer for guests in Azura accommodation.", "None"),
]
FAQS = [
    ("Trip", "What are the trip dates?", "Wednesday 6 to Monday 11 January 2027. Thursday 7 January is the Crown Forest night, included in Core. Monday is checkout and departure."),
    ("Trip", "What does the Core ticket include?", "The welcome and farewell dinners, Cape Coast Castle, Lemon Beach lunch, the Academy visit, the Crown Forest night with breakfast and activities, the Boti Falls excursion and the Accra city tour with lunch. Drinks at Bella Afrik are self-pay."),
    ("Trip", "Will Michael Blackson be there the whole time?", "Michael's planned appearances are the Wednesday welcome dinner, the Thursday Academy visit and the Friday ENZO VIP night. Timings are subject to final confirmation, and a Jubilee House visit is possible but not confirmed."),
    ("Stays", "Do I have to book accommodation with you?", "No. You can arrange your own. If you add an Airbnb or a Four Points room, every guest in it needs their own Core ticket."),
    ("Stays", "How is accommodation priced?", "Per unit, for a fixed package. Airbnb is five nights, 6 to 11 January, and stays yours on the Crown Forest night for your luggage. Four Points is four paid nights, 6 and 8 to 10 January. A one-bedroom Airbnb sleeps up to 2 and a two-bedroom up to 4, and the unit is charged once however many guests share it."),
    ("Stays", "Can I book just an Airbnb, without the event?", "Yes. Choose your own check-in and check-out dates in January 2027, at the same nightly rate. A stay-only booking has no Core ticket, but includes at least one experience, such as the ENZO VIP night or the Waterfall Massage. You can add an airport transfer."),
    ("Stays", "Is breakfast included?", "Breakfast at Crown Forest on 7 January is included in Core. Four Points breakfast is still being confirmed with the hotel."),
    ("Prices", "Are there any discounts?", "Yes. Groups of 4 to 9 get 5% off the total price and groups of 10 or more get 7%. Bookings linked to the same lead count as one group, and the discount is shown before you pay. A booking keeps the discount it was paid at: if your group grows later, the new booking gets the higher rate and earlier ones stay as they are."),
    ("Prices", "Can my partner and I choose different experiences?", "Yes. Experiences are chosen per guest. ENZO VIP is for guests aged 18 and over."),
    ("Prices", "Do you offer airport transfers?", "Yes, for guests staying in Azura accommodation: $100 per guest for an arrival pickup or $200 per guest for pickup and drop-off."),
    ("Payments", "How do deposits and payments work?", "Core tickets, experiences and transfers are paid in full when you book. For accommodation you can pay at least 50% to secure the room and the rest in instalments. Everything must be paid before the first activity on 6 January, and we remind you 30, 14 and 7 days before."),
    ("Payments", "Can each member of our group pay separately?", "Yes. One organiser can pay for everyone, or each guest can pay their own share under the same group booking."),
    ("Payments", "What happens if a balance isn't paid?", "Every guest's share must be paid before the first activity on 6 January. Guests with an unpaid balance at the start can't receive a badge or join activities, and unpaid bookings are forfeited under the booking terms. We never cancel early just because one instalment was missed."),
    ("Payments", "What if my payment fails?", "Nothing is booked until payment is verified, and you won't be charged twice. You can retry straight away from the payment page or your booking link."),
    ("Payments", "What is the cancellation policy?", "Refunds of eligible payments depend on notice: 90% at 60 days or more, 60% at 30 to 59 days, 40% at 14 to 29 days and none under 14 days. See our refund policy for the full terms."),
    ("Travel", "Do you need my passport?", "Only when a confirmed supplier or legal requirement needs it."),
]


class Command(BaseCommand):
    help = "Apply the Change Request v4 catalogue to the flagship package (component pricing)."

    def add_arguments(self, parser):
        parser.add_argument("--content", action="store_true",
                            help="Also replace overview, inclusions, programme and FAQs (overwrites admin edits).")

    @transaction.atomic
    def handle(self, *args, **options):
        retired = TravelPackage.objects.filter(slug__in=RETIRED_SLUGS, is_active=True).update(is_active=False)
        if retired:
            self.stdout.write(f"Switched off {retired} package(s) not in the final change request.")
        pkg = TravelPackage.objects.filter(slug=SLUG).first()
        if pkg is None:
            self.stderr.write("Run seed_michael_blackson first — the flagship package does not exist.")
            return

        pkg.pricing_model = TravelPackage.PricingModel.COMPONENT
        pkg.base_price_per_person = Decimal("999.00")
        pkg.available_from = date(2027, 1, 6)
        pkg.available_to = date(2027, 1, 11)
        pkg.duration_days = 6
        pkg.allow_installments = True
        pkg.accommodation_deposit_percent = Decimal("50.00")
        pkg.final_payment_deadline = date(2027, 1, 5)  # settled before the first 6 January activity
        pkg.save()

        for order, (code, name, kind, cap, min_occ, price, nights, on_sale, units, note, *only) in enumerate(STAYS):
            fields = dict(kind=kind, capacity=cap, min_occupancy=min_occ, price_per_night=Decimal(price),
                          nights=nights, order=order, stay_only=bool(only),
                          check_in=STAY_ONLY_WINDOW[0] if only else None,
                          check_out_by=STAY_ONLY_WINDOW[1] if only else None)
            st, _ = StayType.objects.update_or_create(
                package=pkg, code=code, defaults=fields,
                create_defaults=dict(name=name, on_sale=on_sale, note=note, **fields),
            )
            if not st.units.exists():
                RoomUnit.objects.bulk_create(
                    RoomUnit(stay_type=st, label=f"{name} {i + 1}") for i in range(units)
                )

        experiences, _ = PackageAddonGroup.objects.get_or_create(
            package=pkg, name="Experiences", defaults=dict(selection=PackageAddonGroup.Selection.MULTI, order=2),
        )
        for order, (code, name, price, unit, adults_only, requires_stay, desc, *per) in enumerate(EXPERIENCES, start=1):
            fields = dict(price=Decimal(price), unit=unit, adults_only=adults_only, requires_stay=requires_stay,
                          vehicle_capacity=per[0] if per else 2, group=experiences, order=order, is_active=True,
                          description=desc)
            PackageAddon.objects.update_or_create(
                package=pkg, code=code, defaults=fields, create_defaults=dict(name=name, **fields),
            )
        retired = pkg.addons.exclude(code__in=[c for c, *_ in EXPERIENCES]).update(is_active=False)

        pkg.discount_rules.filter(scope=PackageDiscountRule.Scope.CART).update(is_active=False)
        for name, pct, min_n in GROUP_DISCOUNTS:
            PackageDiscountRule.objects.update_or_create(
                package=pkg, scope=PackageDiscountRule.Scope.GROUP_ACCOMMODATION, min_travellers=min_n,
                defaults=dict(is_active=True), create_defaults=dict(name=name, percent=Decimal(pct), is_active=True),
            )
        # FAQs that state prices or booking rules follow the catalogue on every
        # run; the rest of the copy only changes with --content.
        pkg.faqs.filter(question="What does the $899 Core ticket include?").update(question="What does the Core ticket include?")
        for topic, q, a in FAQS:
            if topic in ("Prices", "Stays"):
                PackageFAQ.objects.update_or_create(package=pkg, question=q, defaults=dict(topic=topic, answer=a),
                                                    create_defaults=dict(topic=topic, answer=a, order=FAQS.index((topic, q, a))))

        if options["content"]:
            self._content(pkg)

        from bookings.pricing import catalog_snapshot
        from bookings.services import publish_price_list
        from packages.models import PriceListVersion
        live = pkg.price_lists.filter(status=PriceListVersion.Status.LIVE).order_by("-number").first()
        if live is None or live.snapshot != catalog_snapshot(pkg):
            v = publish_price_list(pkg, note="Change Request v4 catalogue")
            self.stdout.write(f"Published price list v{v.number}.")

        self.stdout.write(self.style.SUCCESS(
            f"{pkg.title}: component pricing, Core ${pkg.base_price_per_person}, "
            f"{pkg.stay_types.count()} stay types, {len(EXPERIENCES)} experiences and transfers, "
            f"{retired} old add-ons retired."
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
        for code, name, _, _, _, _, desc, *_ in EXPERIENCES:
            pkg.addons.filter(code=code).update(name=name, description=desc)
        for code, name, *_ in STAYS:
            pkg.stay_types.filter(code=code).update(name=name)
        pkg.faqs.all().delete()
        PackageFAQ.objects.bulk_create(
            PackageFAQ(package=pkg, topic=topic, question=q, answer=a, order=i) for i, (topic, q, a) in enumerate(FAQS)
        )
        if not pkg.images.exists():
            for order, (name, caption) in enumerate(PHOTOS):
                with open(SEED_MEDIA / name, "rb") as f:
                    PackageImage.objects.create(package=pkg, image=File(f, name=name), caption=caption,
                                                is_cover=order == 0, order=order)
            self.stdout.write(f"Added {len(PHOTOS)} starter photos.")
        self.stdout.write(f"Content replaced: {len(PROGRAMME)} programme days, {len(FAQS)} FAQs.")
