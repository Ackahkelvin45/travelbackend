"""Tests for booking domain extensions: snapshots, policies, expiry, deadlines."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from packages.models import PackageOption, TravelPackage
from .models import Booking, PolicyDocument


def make_package(**kwargs):
    defaults = dict(
        title="Flagship Tour",
        slug=f"flagship-{TravelPackage.objects.count()}",
        category=TravelPackage.Category.LUXURY_TRAVEL,
        description="x",
        duration_days=10,
        currency="USD",
        available_from=date(2027, 1, 4),
        available_to=date(2027, 1, 13),
    )
    defaults.update(kwargs)
    return TravelPackage.objects.create(**defaults)


class PackageOptionTests(TestCase):
    def test_effective_price_respects_early_bird_deadline(self):
        package = make_package(early_bird_deadline=timezone.now() + timedelta(days=30))
        option = PackageOption.objects.create(
            package=package,
            hotel_name="Accra Marriott",
            star_rating=5,
            occupancy=PackageOption.Occupancy.DOUBLE,
            price_per_person=Decimal("2500.00"),
            early_bird_price_per_person=Decimal("2200.00"),
        )
        price, applied = option.effective_price()
        self.assertEqual(price, Decimal("2200.00"))
        self.assertTrue(applied)

        after = timezone.now() + timedelta(days=31)
        price, applied = option.effective_price(at=after)
        self.assertEqual(price, Decimal("2500.00"))
        self.assertFalse(applied)

    def test_from_price_uses_cheapest_active_option(self):
        package = make_package(early_bird_deadline=timezone.now() + timedelta(days=30))
        PackageOption.objects.create(
            package=package, hotel_name="Four Points", star_rating=4,
            occupancy=PackageOption.Occupancy.DOUBLE,
            price_per_person=Decimal("2000.00"),
            early_bird_price_per_person=Decimal("1800.00"),
        )
        PackageOption.objects.create(
            package=package, hotel_name="Accra Marriott", star_rating=5,
            occupancy=PackageOption.Occupancy.SINGLE,
            price_per_person=Decimal("3000.00"),
        )
        self.assertEqual(package.from_price, Decimal("1800.00"))
        self.assertTrue(package.has_options)


class BookingGuardTests(TestCase):
    def test_option_booking_without_pricing_raises(self):
        package = make_package()
        option = PackageOption.objects.create(
            package=package, hotel_name="Four Points", star_rating=4,
            occupancy=PackageOption.Occupancy.SINGLE,
            price_per_person=Decimal("2000.00"),
        )
        with self.assertRaises(ValueError):
            Booking.objects.create(
                package=package, option=option,
                first_name="A", last_name="B", email="a@b.com",
                travel_date=date(2027, 1, 4),
                unit_price=None, total_amount=None,
            )

    def test_legacy_booking_fallback_still_works(self):
        package = make_package(price_shared=Decimal("600.00"), currency="GHS")
        booking = Booking.objects.create(
            package=package,
            first_name="A", last_name="B", email="a@b.com",
            num_guests=2, travel_date=date(2026, 9, 1),
        )
        self.assertEqual(booking.total_amount, Decimal("1200.00"))


@override_settings(PENDING_BOOKING_TTL_HOURS=24)
class ExpiryAndDeadlineTests(TestCase):
    def _option_booking(self, **kwargs):
        package = make_package(final_payment_deadline=date(2026, 11, 15))
        option = PackageOption.objects.create(
            package=package, hotel_name="Four Points", star_rating=4,
            occupancy=PackageOption.Occupancy.SINGLE,
            price_per_person=Decimal("2000.00"),
        )
        defaults = dict(
            package=package, option=option,
            first_name="A", last_name="B", email="a@b.com",
            travel_date=date(2027, 1, 4),
            unit_price=Decimal("2000.00"), total_amount=Decimal("2000.00"),
            currency="USD",
        )
        defaults.update(kwargs)
        return Booking.objects.create(**defaults)

    def test_unpaid_option_booking_expires_lazily(self):
        booking = self._option_booking()
        self.assertFalse(booking.is_expired)
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        booking.refresh_from_db()
        self.assertTrue(booking.is_expired)

    def test_booking_with_payment_never_expires(self):
        booking = self._option_booking()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=25),
            amount_paid=Decimal("1000.00"),
        )
        booking.refresh_from_db()
        self.assertFalse(booking.is_expired)

    def test_effective_deadline_is_live_with_override(self):
        booking = self._option_booking()
        self.assertEqual(booking.effective_payment_deadline, date(2026, 11, 15))

        # Admin extends the package deadline — applies to everyone instantly.
        booking.package.final_payment_deadline = date(2026, 11, 30)
        booking.package.save()
        booking.refresh_from_db()
        self.assertEqual(booking.effective_payment_deadline, date(2026, 11, 30))

        # Support grants this booking extra time — override wins.
        booking.payment_deadline_override = date(2026, 12, 10)
        booking.save()
        self.assertEqual(booking.effective_payment_deadline, date(2026, 12, 10))


class PolicyDocumentTests(TestCase):
    def test_published_document_is_immutable(self):
        doc = PolicyDocument.objects.create(
            type=PolicyDocument.Type.TERMS, version="1.0",
            title="Terms", body="Original text",
            is_current=True, published_at=timezone.now(),
        )
        doc.body = "Sneaky edit"
        with self.assertRaises(ValueError):
            doc.save()

    def test_only_one_current_version_per_type(self):
        v1 = PolicyDocument.objects.create(
            type=PolicyDocument.Type.REFUND, version="1.0",
            title="Refunds", body="v1", is_current=True,
        )
        PolicyDocument.objects.create(
            type=PolicyDocument.Type.REFUND, version="2.0",
            title="Refunds", body="v2", is_current=True,
        )
        v1.refresh_from_db()
        self.assertFalse(v1.is_current)
        self.assertEqual(
            PolicyDocument.objects.filter(
                type=PolicyDocument.Type.REFUND, is_current=True
            ).count(),
            1,
        )


# ── Phase 3: pricing engine + checkout endpoint ──────────────────────────────

from unittest.mock import patch

from rest_framework.test import APIClient

from .pricing import QuoteError, compute_quote
from .services import create_option_booking, PolicyAcceptanceRequired


def make_flagship(**overrides):
    defaults = dict(
        early_bird_deadline=timezone.now() + timedelta(days=60),
        allow_installments=True,
        deposit_minimum=Decimal("1000.00"),
        # USD-priced package: manual FX mode in tests (no network); Paystack GH
        # charges GHS at this admin-set rate.
        fx_mode="manual",
        charge_exchange_rate=Decimal("15.5000"),
        final_payment_deadline=date(2026, 11, 15),
        visa_addon_enabled=True,
        visa_fee=Decimal("150.00"),
        refund_tiers=[
            {"min_days": 60, "percent": 90},
            {"min_days": 30, "percent": 60},
            {"min_days": 14, "percent": 40},
            {"min_days": 0, "percent": 0},
        ],
    )
    defaults.update(overrides)
    package = make_package(**defaults)
    double = PackageOption.objects.create(
        package=package, hotel_name="Accra Marriott", star_rating=5,
        occupancy=PackageOption.Occupancy.DOUBLE,
        price_per_person=Decimal("2500.00"),
        early_bird_price_per_person=Decimal("2200.00"),
    )
    single = PackageOption.objects.create(
        package=package, hotel_name="Four Points by Sheraton", star_rating=4,
        occupancy=PackageOption.Occupancy.SINGLE,
        price_per_person=Decimal("2000.00"),
        early_bird_price_per_person=Decimal("1800.00"),
    )
    return package, single, double


CONTACT = dict(first_name="Ama", last_name="Owusu", email="ama@example.com")


class PricingEngineTests(TestCase):
    def test_double_occupancy_with_visa_and_early_bird(self):
        _, _, double = make_flagship()
        quote = compute_quote(double, visa=True, payment_plan="installment")

        self.assertEqual(quote.num_guests, 2)
        self.assertTrue(quote.early_bird_applied)
        self.assertEqual(quote.base_total, Decimal("4400.00"))          # 2200 × 2
        self.assertEqual(quote.early_bird_discount, Decimal("600.00"))  # (2500-2200) × 2
        self.assertEqual(quote.addons_total, Decimal("300.00"))         # visa 150 × 2
        self.assertEqual(quote.total, Decimal("4700.00"))
        self.assertEqual(quote.deposit_required, Decimal("1000.00"))
        self.assertEqual(quote.amount_due_today, Decimal("1000.00"))
        self.assertFalse(quote.addons[0]["refundable"])

    def test_full_plan_due_today_is_total(self):
        _, single, _ = make_flagship()
        quote = compute_quote(single, visa=False, payment_plan="full")
        self.assertEqual(quote.total, Decimal("1800.00"))
        self.assertEqual(quote.amount_due_today, Decimal("1800.00"))
        self.assertIsNone(quote.deposit_required)

    def test_early_bird_expired_uses_standard_price(self):
        package, single, _ = make_flagship(
            early_bird_deadline=timezone.now() - timedelta(minutes=1)
        )
        quote = compute_quote(single, visa=False, payment_plan="full")
        self.assertFalse(quote.early_bird_applied)
        self.assertEqual(quote.total, Decimal("2000.00"))
        self.assertEqual(quote.early_bird_discount, Decimal("0.00"))

    def test_deposit_clamped_to_total_for_cheap_bookings(self):
        package, single, _ = make_flagship(deposit_minimum=Decimal("5000.00"))
        quote = compute_quote(single, visa=False, payment_plan="installment")
        self.assertEqual(quote.deposit_required, quote.total)

    def test_installment_refused_when_disabled(self):
        package, single, _ = make_flagship(allow_installments=False)
        with self.assertRaises(QuoteError):
            compute_quote(single, visa=False, payment_plan="installment")

    def test_visa_refused_when_disabled(self):
        package, single, _ = make_flagship(visa_addon_enabled=False)
        with self.assertRaises(QuoteError):
            compute_quote(single, visa=True, payment_plan="full")


class CheckoutServiceTests(TestCase):
    def _publish_policies(self):
        docs = []
        for t in ["terms", "installment", "refund", "privacy"]:
            docs.append(PolicyDocument.objects.create(
                type=t, version="1.0", title=t, body=t,
                is_current=True, published_at=timezone.now(),
            ))
        return docs

    def test_booking_snapshots_everything(self):
        self._publish_policies()
        package, _, double = make_flagship()
        booking = create_option_booking(
            option=double, visa=True, payment_plan="installment",
            contact=CONTACT, accepted_policy_types=["terms", "installment", "refund", "privacy"],
        )
        self.assertEqual(booking.total_amount, Decimal("4700.00"))
        self.assertEqual(booking.deposit_required, Decimal("1000.00"))
        self.assertEqual(booking.option_snapshot["hotel_name"], "Accra Marriott")
        self.assertEqual(booking.travel_date, date(2027, 1, 4))
        self.assertEqual(len(booking.refund_tiers_snapshot), 4)
        self.assertEqual(booking.policy_acceptances.count(), 4)
        self.assertTrue(booking.early_bird_applied)

        # Snapshot survives later admin repricing.
        double.price_per_person = Decimal("9999.00")
        double.early_bird_price_per_person = Decimal("9000.00")
        double.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("4700.00"))

    def test_missing_policy_acceptance_rejected(self):
        self._publish_policies()
        _, single, _ = make_flagship()
        with self.assertRaises(PolicyAcceptanceRequired) as ctx:
            create_option_booking(
                option=single, visa=False, payment_plan="full",
                contact=CONTACT, accepted_policy_types=["terms"],
            )
        self.assertIn("refund", ctx.exception.missing_types)

    def test_no_published_policies_means_nothing_required(self):
        _, single, _ = make_flagship()
        booking = create_option_booking(
            option=single, visa=False, payment_plan="full",
            contact=CONTACT, accepted_policy_types=[],
        )
        self.assertEqual(booking.policy_acceptances.count(), 0)


class CheckoutEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_price_mismatch_returns_409_with_fresh_quote(self):
        _, single, _ = make_flagship()
        r = self.client.post("/api/bookings/checkout/", {
            "option_id": str(single.id),
            "visa": False,
            "payment_plan": "full",
            "expected_total": "1234.00",  # stale page
            **CONTACT,
        }, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["quote"]["total"], "1800.00")

    def test_checkout_creates_booking(self):
        _, _, double = make_flagship()
        r = self.client.post("/api/bookings/checkout/", {
            "option_id": str(double.id),
            "visa": True,
            "payment_plan": "installment",
            "expected_total": "4700.00",
            **CONTACT,
        }, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["amount_due_today"], "1000.00")
        self.assertTrue(r.data["early_bird_applied"])

    def test_pricing_matrix_endpoint(self):
        package, _, _ = make_flagship()
        r = self.client.get(f"/api/packages/{package.id}/pricing/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["options"]), 2)
        self.assertTrue(r.data["early_bird"]["active"])
        self.assertTrue(r.data["installments"]["enabled"])
        self.assertEqual(r.data["visa"]["fee_per_guest"], "150.00")
        self.assertIn("server_now", r.data)


class InitializeIntentTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _installment_booking(self):
        _, _, double = make_flagship()
        return create_option_booking(
            option=double, visa=True, payment_plan="installment",
            contact=CONTACT, accepted_policy_types=[],
        )

    @patch("payments.views.initialize_transaction")
    def test_deposit_intent_charges_outstanding_deposit(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._installment_booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "intent": "deposit",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["amount"], "1000.00")
        self.assertEqual(booking.payments.get().purpose, "deposit")

    @patch("payments.views.initialize_transaction")
    def test_custom_intent_clamped_to_balance(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._installment_booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "intent": "custom", "amount": "999999.00",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["amount"], "4700.00")  # clamped to balance

    @patch("payments.views.initialize_transaction")
    def test_expired_booking_cannot_initialize(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._installment_booking()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id),
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.data["detail"])

    @patch("payments.views.initialize_transaction")
    def test_deposit_payment_confirms_installment_booking(self, mock_init):
        """End-to-end: deposit initialize → gateway success → booking confirmed
        with balance outstanding."""
        from payments.money import to_subunits
        from payments.services import apply_successful_payment

        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._installment_booking()
        self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "intent": "deposit",
        }, format="json")

        payment = booking.payments.get()
        # The gateway reports what was actually charged (GHS for USD bookings).
        with patch("payments.email.send_booking_confirmation"):
            result = apply_successful_payment(payment, {
                "amount": to_subunits(payment.charged_amount or payment.amount),
                "currency": payment.charged_currency or payment.currency,
                "status": "success",
            })

        booking.refresh_from_db()
        self.assertTrue(result.promoted)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)  # deposit confirms
        self.assertEqual(booking.balance, Decimal("3700.00"))       # balance remains
        self.assertFalse(booking.is_paid)


class DayTourBookingTests(TestCase):
    """Flat-price day tours booked on scheduled departures: date must be valid,
    seats never oversell, and non-departure packages keep the old behaviour."""

    def _day_tour(self, capacity=None):
        from packages.models import TourDeparture
        pkg = make_package(
            title="Accra City Tour", slug=f"accra-{TravelPackage.objects.count()}",
            category=TravelPackage.Category.CULTURAL, duration_days=1, currency="GHS",
            price_shared=Decimal("750.00"), is_day_tour=True,
            available_from=None, available_to=None,
        )
        dep = TourDeparture.objects.create(
            package=pkg, date=date(2027, 3, 6), capacity=capacity, is_active=True)
        return pkg, dep

    def _book(self, pkg, travel_date=None, guests=1, email="tour@test.com"):
        payload = {"package_id": str(pkg.id), "price_tier": "shared",
                   "first_name": "Kofi", "last_name": "Test", "email": email,
                   "num_guests": guests}
        if travel_date is not None:
            payload["travel_date"] = str(travel_date)
        return APIClient().post("/api/bookings/", payload, format="json")

    def test_booking_requires_a_departure_date(self):
        pkg, _ = self._day_tour()
        r = self._book(pkg, travel_date=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn("departure date", str(r.data["travel_date"]).lower())

    def test_booking_rejects_a_non_departure_date(self):
        pkg, _ = self._day_tour()
        r = self._book(pkg, travel_date=date(2027, 12, 25))
        self.assertEqual(r.status_code, 400)

    def test_booking_on_valid_departure_succeeds_and_reserves_seats(self):
        pkg, dep = self._day_tour(capacity=10)
        r = self._book(pkg, travel_date=dep.date, guests=2)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["total_amount"], "1500.00")   # 750 × 2
        dep.refresh_from_db()
        self.assertEqual(dep.seats_taken, 2)

    def test_departure_never_oversells(self):
        pkg, dep = self._day_tour(capacity=3)
        self.assertEqual(self._book(pkg, dep.date, guests=2).status_code, 201)  # 2/3
        # 2 more would exceed 3 → rejected, seats unchanged
        r = self._book(pkg, dep.date, guests=2)
        self.assertEqual(r.status_code, 400)
        dep.refresh_from_db()
        self.assertEqual(dep.seats_taken, 2)
        # exactly 1 fits → full
        self.assertEqual(self._book(pkg, dep.date, guests=1).status_code, 201)
        dep.refresh_from_db()
        self.assertTrue(dep.is_full)

    def test_unlimited_capacity_departure(self):
        pkg, dep = self._day_tour(capacity=None)
        self.assertEqual(self._book(pkg, dep.date, guests=50).status_code, 201)
        dep.refresh_from_db()
        self.assertIsNone(dep.seats_left)   # unlimited
        self.assertFalse(dep.is_full)

    def test_non_departure_package_unaffected(self):
        # A normal legacy package (no departures) still books with any/no date.
        pkg = make_package(price_shared=Decimal("600.00"), currency="GHS")
        r = self._book(pkg, travel_date=None)   # date optional, defaults to available_from
        self.assertEqual(r.status_code, 201)

    def test_api_exposes_day_tour_fields_and_departures(self):
        pkg, dep = self._day_tour(capacity=5)
        pkg.price_usd_estimate = Decimal("65.00"); pkg.save()
        r = APIClient().get(f"/api/packages/{pkg.id}/")
        self.assertTrue(r.data["is_day_tour"])
        self.assertEqual(str(r.data["price_usd_estimate"]), "65.00")
        self.assertEqual(len(r.data["departures"]), 1)
        self.assertEqual(r.data["departures"][0]["seats_left"], 5)


class ConfigurablePricingTests(TestCase):
    """Core-tour + add-ons pricing: highest-wins non-stacking discount, group
    rules, and correct booking snapshots. Prices verified against the guide."""

    def _package(self):
        from packages.models import PackageAddon, PackageAddonGroup, PackageDiscountRule
        pkg = make_package(
            title="MB Experience", slug=f"mb-{TravelPackage.objects.count()}",
            currency="USD", duration_days=8, allow_installments=True,
            deposit_minimum=Decimal("1000.00"),
            pricing_model=TravelPackage.PricingModel.CORE_PLUS_ADDONS,
            base_price_per_person=Decimal("878.00"),
        )
        hotels = PackageAddonGroup.objects.create(package=pkg, name="Hotel", selection="single", required=False)
        exp = PackageAddonGroup.objects.create(package=pkg, name="Experiences", selection="multi")
        FLAT, PP = PackageAddon.Unit.PER_BOOKING, PackageAddon.Unit.PER_PERSON
        a = lambda code, price, unit, group, default=False: PackageAddon.objects.create(
            package=pkg, code=code, name=code, price=Decimal(price), unit=unit, group=group, is_default=default)
        no = a("no_hotel", "0.00", FLAT, hotels, default=True)
        mid = a("hotel_mid", "2000.00", FLAT, hotels)
        prem = a("hotel_premium", "3200.00", FLAT, hotels)
        kozo = a("kozo", "100.00", PP, exp); enzo = a("enzo", "133.00", PP, exp); mas = a("mas", "107.00", PP, exp)
        def rule(name, pct, req):
            r = PackageDiscountRule.objects.create(package=pkg, name=name, percent=Decimal(pct)); r.required_addons.set(req)
        rule("all extras", "5", [kozo, enzo, mas]); rule("mid", "7", [mid])
        rule("mid+extras", "10", [mid, kozo, enzo, mas]); rule("premium", "12", [prem])
        rule("premium+extras", "15", [prem, kozo, enzo, mas])
        return pkg

    def _quote(self, pkg, codes, guests=1, plan="full"):
        from bookings.pricing import compute_configurable_quote
        return compute_configurable_quote(pkg, num_guests=guests, selected_addon_codes=codes, payment_plan=plan)

    def test_totals_match_the_guide(self):
        pkg = self._package()
        cases = {
            (): "878.00", ("kozo", "enzo", "mas"): "1157.10",
            ("hotel_mid",): "2676.54", ("hotel_mid", "kozo", "enzo", "mas"): "2896.20",
            ("hotel_premium",): "3588.64", ("hotel_premium", "kozo", "enzo", "mas"): "3755.30",
        }
        for codes, expected in cases.items():
            self.assertEqual(str(self._quote(pkg, list(codes)).total), expected, codes)

    def test_discount_never_stacks_highest_wins(self):
        pkg = self._package()
        q = self._quote(pkg, ["hotel_mid", "kozo", "enzo", "mas"])
        self.assertEqual(q.discount_percent, Decimal("10.00"))  # not 5+7

    def test_single_select_group_rejects_two(self):
        from bookings.pricing import QuoteError
        pkg = self._package()
        with self.assertRaises(QuoteError):
            self._quote(pkg, ["hotel_mid", "hotel_premium"])

    def test_unknown_addon_rejected(self):
        from bookings.pricing import QuoteError
        with self.assertRaises(QuoteError):
            self._quote(self._package(), ["nope"])

    def test_no_hotel_default_auto_applied(self):
        pkg = self._package()
        q = self._quote(pkg, [])  # nothing chosen
        self.assertIn("no_hotel", {a["code"] for a in q.addons})
        self.assertEqual(q.total, Decimal("878.00"))

    def test_per_person_addons_scale_with_guests(self):
        pkg = self._package()
        q = self._quote(pkg, ["kozo"], guests=2)   # 878×2 + 100×2 = 1956, no discount
        self.assertEqual(q.base_total, Decimal("1756.00"))
        self.assertEqual(q.addons_total, Decimal("200.00"))

    def test_checkout_creates_snapshotted_booking(self):
        pkg = self._package()
        r = APIClient().post("/api/bookings/checkout/configurable/", {
            "package_id": str(pkg.id), "num_guests": 1,
            "selected_addon_codes": ["hotel_mid", "kozo", "enzo", "mas"],
            "payment_plan": "installment", "first_name": "M", "last_name": "B",
            "email": "mb@test.com", "accepted_policies": [],
        }, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["total_amount"], "2896.20")
        self.assertEqual(r.data["discount_amount"], "321.80")
        b = Booking.objects.get(reference=r.data["reference"])
        self.assertEqual(b.discount_amount, Decimal("321.80"))
        self.assertIn("10.00%", b.discount_note)
        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(b)["reconciles"])


# ── Policy-guide acceptance checks (Appendix D) ──────────────────────────────

class DepositAndDeadlineTests(TestCase):
    """The initial-payment and deadline rules from the customer-policies guide."""

    def _pkg(self, **kw):
        from packages.models import PackageAddon, PackageAddonGroup
        pkg = make_package(
            title="MB", slug=f"mb-dep-{TravelPackage.objects.count()}",
            currency="USD", duration_days=8, allow_installments=True,
            deposit_minimum=Decimal("1000.00"),
            pricing_model=TravelPackage.PricingModel.CORE_PLUS_ADDONS,
            base_price_per_person=Decimal("878.00"), **kw,
        )
        hotels = PackageAddonGroup.objects.create(package=pkg, name="Hotel", selection="single")
        PackageAddon.objects.create(package=pkg, group=hotels, code="no_hotel", name="No Hotel",
                                    price=Decimal("0.00"), unit="per_booking", is_default=True)
        mid = PackageAddon.objects.create(package=pkg, group=hotels, code="hotel_mid", name="Mid",
                                          price=Decimal("2000.00"), unit="per_booking")
        from packages.models import PackageDiscountRule
        rule = PackageDiscountRule.objects.create(package=pkg, name="Core + Mid", percent=Decimal("7.00"))
        rule.required_addons.set([mid])
        return pkg

    def _quote(self, pkg, guests=1, codes=(), plan="installment"):
        from bookings.pricing import compute_configurable_quote
        return compute_configurable_quote(
            pkg, num_guests=guests, selected_addon_codes=list(codes), payment_plan=plan,
        )

    def test_core_only_never_charges_more_than_the_package(self):
        # "Core only at $878 → full $878 payment; never a $1,000 charge."
        self.assertEqual(self._quote(self._pkg()).amount_due_today, Decimal("878.00"))

    def test_per_booking_deposit_is_one_capped_minimum(self):
        q = self._quote(self._pkg(), guests=2)          # total 1756, one minimum
        self.assertEqual(q.amount_due_today, Decimal("1000.00"))

    def test_per_traveller_deposit_sums_capped_shares(self):
        # "Two $878 Core travellers → initial requirement $1,756."
        pkg = self._pkg(deposit_unit=TravelPackage.DepositUnit.PER_TRAVELLER)
        self.assertEqual(self._quote(pkg, guests=2).amount_due_today, Decimal("1756.00"))
        # A cart above the minimum per head still pays the minimum each.
        q = self._quote(pkg, guests=2, codes=["hotel_mid"])   # 3493.08 total
        self.assertEqual(q.amount_due_today, Decimal("2000.00"))

    def test_balance_after_deposit(self):
        # "Package $2,676 before due date → minimum $1,000; balance $1,676."
        q = self._quote(self._pkg(), codes=["hotel_mid"])
        self.assertEqual(q.total - q.deposit_required, Decimal("1676.54"))

    def test_installments_closed_on_and_after_the_deadline(self):
        from bookings.pricing import QuoteError, installments_open
        past = self._pkg(final_payment_deadline=date.today())
        self.assertFalse(installments_open(past))          # on the due date → full payment
        with self.assertRaises(QuoteError):
            self._quote(past)
        self.assertEqual(self._quote(past, plan="full").amount_due_today, Decimal("878.00"))
        future = self._pkg(final_payment_deadline=date.today() + timedelta(days=1))
        self.assertTrue(installments_open(future))


class OptionalPolicyTests(TestCase):
    """Optional consents (media) must never block a booking."""

    def test_optional_policy_is_not_required_at_checkout(self):
        from bookings.models import PolicyDocument
        from bookings.services import required_policy_documents
        from django.utils import timezone

        PolicyDocument.objects.create(
            type=PolicyDocument.Type.TERMS, version="1.0", title="Terms", body="t",
            is_current=True, published_at=timezone.now(),
        )
        PolicyDocument.objects.create(
            type=PolicyDocument.Type.MEDIA, version="1.0", title="Media consent", body="m",
            is_current=True, published_at=timezone.now(), is_required=False,
        )
        types = {d.type for d in required_policy_documents()}
        self.assertEqual(types, {"terms"})

        r = APIClient().get("/api/bookings/policies/")
        self.assertEqual({d["type"]: d["is_required"] for d in r.data},
                         {"terms": True, "media": False})


# ── Component pricing (Change Request v2) ─────────────────────────────────────

def compute_component_quote_for(case, mode, n, rooms, **kwargs):
    from .pricing import compute_component_quote
    return compute_component_quote(case.pkg, mode=mode, travellers=[{} for _ in range(n)], rooms=list(rooms),
                                   payment_plan="full", **kwargs)


class ComponentPricingTests(TestCase):
    """The client's acceptance totals and room/ticket rules (Change Request v4 §4–§6, §9)."""

    @classmethod
    def setUpTestData(cls):
        from packages.models import PackageAddon, PackageDiscountRule, RoomUnit, StayType

        cls.pkg = make_package(
            pricing_model=TravelPackage.PricingModel.COMPONENT, base_price_per_person=Decimal("899.00"),
            available_from=date(2027, 1, 6), available_to=date(2027, 1, 11),
            final_payment_deadline=date(2027, 1, 5), allow_installments=True,
        )

        def stay(code, cap, price, nights, units, min_occ=1, on_sale=True, **kw):
            st = StayType.objects.create(
                package=cls.pkg, code=code, name=code, kind="airbnb" if "airbnb" in code else "hotel",
                capacity=cap, min_occupancy=min_occ, price_per_night=Decimal(price), nights=nights, on_sale=on_sale,
                **kw,
            )
            for i in range(units):
                RoomUnit.objects.create(stay_type=st, label=f"{code}-{i}")
            return st

        stay("airbnb_1bed", 2, "220", 5, 2)
        stay("airbnb_2bed", 4, "340", 5, 1)
        stay("fourpoints_standard", 1, "330", 4, 2)
        stay("fourpoints_couple", 2, "380", 4, 1, min_occ=2)
        stay("fp_closed", 2, "999", 4, 1, on_sale=False)
        stay("airbnb_1bed_stay", 2, "220", 11, 1, stay_only=True, check_in=date(2027, 1, 20))

        def addon(code, price, **kw):
            return PackageAddon.objects.create(package=cls.pkg, code=code, name=code, price=Decimal(price), **kw)

        addon("enzo_vip", "220", adults_only=True)
        addon("waterfall_massage", "100")
        addon("airport_pickup", "40", unit=PackageAddon.Unit.PER_VEHICLE, requires_stay=True)
        addon("airport_return", "70", unit=PackageAddon.Unit.PER_VEHICLE, requires_stay=True)
        for name, pct, n in [("Group of 4–9", "5", 4), ("Group of 10+", "7", 10)]:
            PackageDiscountRule.objects.create(
                package=cls.pkg, name=name, percent=Decimal(pct), min_travellers=n, is_active=False,
                scope=PackageDiscountRule.Scope.GROUP_ACCOMMODATION,
            )

    def quote(self, mode, travellers, rooms=(), plan="full"):
        from .pricing import compute_component_quote
        if isinstance(travellers, int):
            travellers = [{} for _ in range(travellers)]
        return compute_component_quote(self.pkg, mode=mode, travellers=travellers, rooms=list(rooms), payment_plan=plan)

    def assertRejected(self, *args, **kwargs):
        from .pricing import QuoteError
        with self.assertRaises(QuoteError):
            self.quote(*args, **kwargs)

    def test_acceptance_totals(self):
        from .pricing import acceptance_checks
        self.pkg.base_price_per_person = Decimal("999.00")  # the client's current Core price
        self.pkg.save()
        results = acceptance_checks(self.pkg)
        self.assertEqual(len(results), 7)
        for r in results:
            with self.subTest(r["label"]):
                self.assertTrue(r["ok"], r)

    def test_couple_shows_each_share_and_charges_the_room_once(self):
        q = self.quote("couple", 2, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}])
        self.assertEqual(q.total, Decimal("2898.00"))
        self.assertEqual(q.rooms_total, Decimal("1100.00"))
        self.assertEqual([t["share"] for t in q.travellers], ["1449.00", "1449.00"])
        self.assertEqual(q.as_dict()["average_per_person"], "1449.00")

    def test_group_discount_is_off_by_default_and_comes_off_the_whole_price(self):
        """Client, 26 Oct 2026: one group discount, worked on the total price."""
        from packages.models import PackageDiscountRule
        rooms = [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2, 3]}]
        self.assertEqual(self.quote("group", 4, rooms).total, Decimal("5296.00"))  # 4 × 899 + 1,700

        PackageDiscountRule.objects.filter(package=self.pkg).update(is_active=True)
        self.pkg.price_lists.all().delete()  # unpublished: quotes read the draft tables
        q = self.quote("group", 4, rooms)
        self.assertEqual(q.discount_total, Decimal("264.80"))  # 5% of 5,296
        self.assertEqual(q.total, Decimal("5031.20"))
        self.assertEqual(sum(Decimal(t["share"]) for t in q.travellers), q.total)
        self.assertEqual(self.quote("group", 4).total, Decimal("3416.20"))  # Core only: 5% off 3,596
        self.assertEqual(self.quote("group", 3, [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}]).discount_total,
                         Decimal("0.00"))  # under 4: none
        self.assertEqual(q.discount_note(), "Group of 4–9 (5% off the total)")

        # A linked booking counts the whole group: 4 guests here, 10 in the group → 7%.
        q10 = compute_component_quote_for(self, "group", 4, rooms, group_size=10)
        self.assertEqual(q10.discount_total, Decimal("370.72"))  # 7% of 5,296
        self.assertEqual(q10.total, Decimal("4925.28"))

    def test_shares_add_up_to_the_total(self):
        q = self.quote("group", 3, [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}])  # 1,700 / 3
        self.assertEqual([t["room_share"] for t in q.travellers], ["566.67", "566.67", "566.66"])
        self.assertEqual(sum(Decimal(t["share"]) for t in q.travellers), q.total)

    def test_adults_only_experience_needs_confirmation(self):
        self.assertRejected("single", [{"experiences": ["enzo_vip"]}])
        q = self.quote("single", [{"experiences": ["enzo_vip"], "adult": True}])
        self.assertEqual(q.total, Decimal("1119.00"))

    def test_airport_transfers_are_per_vehicle_and_for_stay_guests(self):
        room = [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}]
        self.assertRejected("single", [{"experiences": ["airport_pickup"]}])  # no Azura stay
        q = self.quote("group", [{"experiences": ["airport_pickup"]}] * 3, room)
        self.assertEqual(q.transfers, [{"code": "airport_pickup", "name": "airport_pickup", "guests": [0, 1, 2],
                                        "vehicles": 2, "vehicle_capacity": 2, "price": "40.00", "charge": "80.00",
                                        "refundable": True}])
        self.assertEqual(q.total, Decimal("4477.00"))  # 3 × 899 + 1,700 + 2 vehicles × 40
        self.assertRejected("single", [{"experiences": ["airport_pickup", "airport_return"]}],
                            [{"stay_type": "airbnb_1bed", "occupants": [0]}])

    def test_installments_core_in_full_and_half_the_room(self):
        rooms = [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}]
        q = self.quote("couple", [{"experiences": ["waterfall_massage"]}, {}], rooms, plan="installment")
        self.assertEqual(q.amount_due_today, Decimal("2448.00"))  # 1,798 Core + 100 massage + 550 room
        self.assertEqual(q.deposit_required, q.amount_due_today)
        self.assertEqual([t["due_now"] for t in q.travellers], ["1274.00", "1174.00"])
        self.assertRejected("couple", 2, plan="installment")  # nothing to spread without a room

    def test_one_guest_may_book_a_two_bed_unit(self):
        q = self.quote("single", 1, [{"stay_type": "airbnb_2bed", "occupants": [0]}])
        self.assertEqual(q.total, Decimal("2599.00"))  # 899 + 5 × 340

    def test_room_rules(self):
        self.assertRejected("group", 5, [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2, 3, 4]}])
        self.assertRejected("group", 3, [{"stay_type": "airbnb_1bed", "occupants": [0, 1, 2]}])
        # Every occupant must be a traveller (= hold Core); nobody in two rooms.
        self.assertRejected("couple", 2, [{"stay_type": "airbnb_1bed", "occupants": [0, 2]}])
        self.assertRejected("group", 3, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]},
                                         {"stay_type": "fourpoints_standard", "occupants": [1]}])
        # One Airbnb per booking, and it can't leave a guest without a room.
        self.assertRejected("group", 4, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]},
                                         {"stay_type": "airbnb_1bed", "occupants": [2, 3]}])
        self.assertRejected("group", 3, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}])
        # Standard rooms stay single (a couple there takes two rooms); the couple room needs two.
        self.assertRejected("couple", 2, [{"stay_type": "fourpoints_standard", "occupants": [0, 1]}])
        two_rooms = self.quote("couple", 2, [{"stay_type": "fourpoints_standard", "occupants": [0]},
                                             {"stay_type": "fourpoints_standard", "occupants": [1]}])
        self.assertEqual(two_rooms.total, Decimal("4438.00"))  # 2 × (899 + 4 × 330)
        self.assertRejected("group", 1, [{"stay_type": "fourpoints_couple", "occupants": [0]}])
        self.assertRejected("single", 1, [{"stay_type": "fp_closed", "occupants": [0]}])
        self.assertRejected("single", 2)
        self.assertRejected("couple", 3)

    def test_stay_only_is_its_own_airbnb_without_core(self):
        room = [{"stay_type": "airbnb_1bed_stay", "occupants": [0, 1]}]
        q = self.quote("stay", 2, room)
        self.assertEqual((q.core_total, q.total), (Decimal("0.00"), Decimal("2420.00")))  # 11 × 220, no Core
        self.assertEqual(q.rooms[0]["check_in"], "2027-01-20")
        q = self.quote("stay", [{"experiences": ["airport_pickup"]}] * 2, room)
        self.assertEqual(q.total, Decimal("2460.00"))  # + one shared vehicle at this fixture's $40
        self.assertRejected("stay", 1)  # needs an Airbnb
        self.assertRejected("stay", 2, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}])  # event-week unit
        self.assertRejected("couple", 2, room)  # stay-only units never come with Core
        self.assertRejected("stay", [{"experiences": ["enzo_vip"], "adult": True}],
                            [{"stay_type": "airbnb_1bed_stay", "occupants": [0]}])  # event experience
        self.assertRejected("stay", 3, [{"stay_type": "airbnb_1bed_stay", "occupants": [0, 1, 2]}])  # doesn't fit

    def test_stay_only_booking_is_dated_from_its_room(self):
        from .pricing import units_left
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed_stay"])
        booking = self.checkout(token, mode="stay", rooms=[{"stay_type": "airbnb_1bed_stay", "occupants": [0, 1]}])
        self.assertEqual(booking.travel_date, date(2027, 1, 20))
        self.assertEqual(units_left(self.pkg, "airbnb_1bed"), 2)  # event-week units untouched

    def test_quote_rejects_sold_out_rooms(self):
        self.assertRejected("group", 2, [{"stay_type": "airbnb_2bed", "occupants": [0]},
                                         {"stay_type": "airbnb_2bed", "occupants": [1]}])

    # ── Holds, checkout, moves ────────────────────────────────────────────────

    def checkout(self, token, mode="couple", n=2, rooms=None, **kwargs):
        from .services import create_component_booking
        rooms = rooms if rooms is not None else [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}]
        return create_component_booking(
            package=self.pkg, mode=mode, travellers=[{"name": f"G{i}"} for i in range(n)], rooms=rooms,
            payment_plan="full", hold_token=token, accepted_policy_types=[],
            contact={"first_name": "Ama", "last_name": "Owusu", "email": "ama@example.com"}, **kwargs,
        )

    def test_hold_blocks_others_until_it_expires(self):
        from .pricing import QuoteError
        from .services import create_hold
        from packages.models import RoomUnit

        token, _, units = create_hold(self.pkg, ["airbnb_2bed"])
        with self.assertRaises(QuoteError):
            create_hold(self.pkg, ["airbnb_2bed"])
        RoomUnit.objects.filter(hold_token=token).update(held_until=timezone.now() - timedelta(seconds=1))
        _, _, again = create_hold(self.pkg, ["airbnb_2bed"])
        self.assertEqual(again[0].pk, units[0].pk)

    def test_own_hold_does_not_count_as_sold_out(self):
        from .pricing import compute_component_quote
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])  # the only 2-bed unit
        rooms = [{"stay_type": "airbnb_2bed", "occupants": [0]}]
        self.assertRejected("single", 1, rooms)
        q = compute_component_quote(self.pkg, mode="single", travellers=[{}], rooms=rooms,
                                    payment_plan="full", hold_token=token)
        self.assertEqual(q.total, Decimal("2599.00"))

    def test_checkout_issues_one_core_ticket_per_traveller(self):
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token)
        self.assertEqual(booking.total_amount, Decimal("2898.00"))
        self.assertEqual(booking.travellers.count(), 2)
        unit = booking.room_units.get()
        self.assertEqual(set(booking.travellers.values_list("unit", flat=True)), {unit.pk})
        self.assertIsNotNone(unit.held_until)  # unpaid window, not yet sold

        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(booking)["reconciles"])  # room line is on the receipt

    def test_checkout_with_expired_hold_is_refused(self):
        from .pricing import QuoteError
        from .services import create_hold
        from packages.models import RoomUnit

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        RoomUnit.objects.filter(hold_token=token).update(held_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(QuoteError):
            self.checkout(token)
        self.assertFalse(Booking.objects.exists())

    def test_move_traveller_never_adds_a_ticket(self):
        from .services import IllegalTransition, create_hold, move_traveller

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed", "fourpoints_standard"])
        booking = self.checkout(token, mode="group", n=3, rooms=[
            {"stay_type": "airbnb_1bed", "occupants": [0, 1]}, {"stay_type": "fourpoints_standard", "occupants": [2]},
        ])
        hotel = booking.room_units.get(stay_type__code="fourpoints_standard")
        g0, g1, g2 = booking.travellers.order_by("created_at", "full_name")
        with self.assertRaises(IllegalTransition):
            move_traveller(g0, hotel)  # standard room already full
        move_traveller(g2, None)
        move_traveller(g0, hotel)
        self.assertEqual(booking.travellers.count(), 3)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("5117.00"))  # 2,898 + 2,219, not re-priced

        other_token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        other = self.checkout(other_token)
        with self.assertRaises(IllegalTransition):
            move_traveller(g1, other.room_units.get())

    def test_payment_sells_rooms_and_cancel_releases_them(self):
        from payments.models import Payment
        from payments.services import apply_successful_payment, generate_payment_reference
        from .services import cancel_booking, create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="single", n=1, rooms=[{"stay_type": "airbnb_2bed", "occupants": [0]}])
        payment = Payment.objects.create(
            booking=booking, paystack_reference=generate_payment_reference(booking),
            amount=booking.total_amount, currency=booking.currency,
        )
        result = apply_successful_payment(payment, None)
        self.assertNotIn("room_lost", result.problems)
        unit = booking.room_units.get()
        self.assertIsNone(unit.held_until)
        self.assertEqual(self.pkg.stay_types.get(code="airbnb_2bed").units_available(), 0)

        cancel_booking(booking, reason=Booking.CancellationReason.ADMIN)
        self.assertEqual(self.pkg.stay_types.get(code="airbnb_2bed").units_available(), 1)

    def test_rooms_are_held_only_for_the_checkout_window(self):
        from packages.models import RoomUnit
        from payments.models import OpsConfig
        from .pricing import QuoteError
        from .services import create_hold, renew_room_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="single", n=1, rooms=[{"stay_type": "airbnb_2bed", "occupants": [0]}])
        unit = RoomUnit.objects.get(booking=booking)
        window = timedelta(minutes=OpsConfig.get().hold_minutes)
        self.assertLessEqual(unit.held_until, timezone.now() + window)  # not the old 24-hour unpaid hold

        RoomUnit.objects.filter(pk=unit.pk).update(held_until=timezone.now() - timedelta(seconds=1))
        renew_room_hold(booking)  # still free: a retry gets a fresh window
        unit.refresh_from_db()
        self.assertGreater(unit.held_until, timezone.now())

        RoomUnit.objects.filter(pk=unit.pk).update(held_until=timezone.now() - timedelta(seconds=1))
        create_hold(self.pkg, ["airbnb_2bed"])  # someone else takes it
        with self.assertRaises(QuoteError):
            renew_room_hold(booking)

    def test_group_members_pay_separately(self):
        from unittest import mock
        from payments.models import Payment
        from payments.services import apply_successful_payment
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="group", n=3, pay_separately=True,
                                rooms=[{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}])
        self.assertEqual(booking.total_amount, Decimal("4397.00"))  # 3 × 899 + 1,700
        # Checkout covers the organiser's whole share plus the others' room shares.
        self.assertEqual(booking.deposit_required, Decimal("2599.00"))  # 1,465.67 + 566.67 + 566.66

        def pay(amount, traveller=None):
            p = Payment.objects.create(booking=booking, paystack_reference=f"T-{Payment.objects.count()}",
                                       amount=Decimal(amount), currency="USD", traveller=traveller)
            apply_successful_payment(p, None)
            booking.refresh_from_db()

        pay("2599.00")
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual([str(o) for *_, o in booking.traveller_balances()], ["0.00", "899.00", "899.00"])

        member = booking.travellers.get(position=1)
        share = self.client.get(f"/api/bookings/share/{member.id}/").json()
        self.assertEqual((share["outstanding"], share["payable"]), ("899.00", True))
        with mock.patch("payments.views.initialize_transaction",
                        return_value={"access_code": "a", "authorization_url": "u"}) as init:
            res = self.client.post("/api/payments/initialize/", {
                "booking_id": str(booking.id), "intent": "traveller", "traveller_id": str(member.id),
                "payer_email": "kojo@example.com",
            }, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["amount"], "899.00")
        self.assertEqual(init.call_args.kwargs["email"], "kojo@example.com")
        self.assertEqual(Payment.objects.get(paystack_reference=res.json()["reference"]).traveller, member)

        pay("899.00", traveller=member)
        self.assertEqual([str(o) for *_, o in booking.traveller_balances()], ["0.00", "0.00", "899.00"])
        self.assertEqual(booking.balance, Decimal("899.00"))

    def test_only_groups_pay_separately(self):
        from .pricing import QuoteError
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        with self.assertRaises(QuoteError):
            self.checkout(token, pay_separately=True)

    def test_payment_after_lapsed_hold_is_flagged(self):
        from payments.models import Payment
        from payments.services import apply_successful_payment, generate_payment_reference
        from packages.models import RoomUnit
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="single", n=1, rooms=[{"stay_type": "airbnb_2bed", "occupants": [0]}])
        RoomUnit.objects.filter(booking=booking).update(held_until=timezone.now() - timedelta(seconds=1))
        create_hold(self.pkg, ["airbnb_2bed"])  # someone else takes the lapsed unit
        payment = Payment.objects.create(
            booking=booking, paystack_reference=generate_payment_reference(booking),
            amount=booking.total_amount, currency=booking.currency,
        )
        result = apply_successful_payment(payment, None)
        self.assertIn("room_lost", result.problems)
        payment.refresh_from_db()
        self.assertTrue(payment.needs_review)

    # ── Versioned price lists ─────────────────────────────────────────────────

    def test_draft_edits_reach_customers_only_after_publishing(self):
        from packages.models import StayType
        from .services import create_hold, publish_price_list

        rooms = [{"stay_type": "airbnb_1bed", "occupants": [0]}]
        v1 = publish_price_list(self.pkg, note="launch")
        StayType.objects.filter(package=self.pkg, code="airbnb_1bed").update(price_per_night=Decimal("250"))
        self.assertEqual(self.quote("single", 1, rooms).total, Decimal("1999.00"))  # still v1

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token, mode="single", n=1, rooms=rooms)
        self.assertEqual(booking.price_list_version, v1)

        v2 = publish_price_list(self.pkg, note="1-bed now $250")
        q = self.quote("single", 1, rooms)
        self.assertEqual(q.total, Decimal("2149.00"))  # 899 + 5 × 250
        self.assertEqual(q.as_dict()["price_list_version"], 2)
        v1.refresh_from_db()
        self.assertEqual((v1.status, v2.status), ("retired", "live"))
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("1999.00"))  # sold price never changes

    # ── API ───────────────────────────────────────────────────────────────────

    def test_owner_can_change_message_preferences(self):
        from django.contrib.auth import get_user_model
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token, marketing_consent=True)
        url = f"/api/bookings/{booking.reference}/preferences/"
        self.assertEqual(self.client.patch(url, {"marketing_consent": False}, content_type="application/json").status_code, 401)

        owner = get_user_model().objects.create_user(email="ama@example.com", password="x-Strong-pass-1", first_name="Ama", last_name="O")
        Booking.objects.filter(pk=booking.pk).update(user=owner)
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(owner)
        res = api.patch(url, {"marketing_consent": False, "photo_consent": True}, format="json")
        self.assertEqual(res.json(), {"marketing_consent": False, "photo_consent": True, "whatsapp_updates": False})
        units = {t["unit"] for t in api.get(f"/api/bookings/{booking.reference}/").json()["travellers"]}
        self.assertEqual(len(units), 1)  # both guests share the one held unit
        self.assertIsNotNone(units.pop())

    def test_quote_endpoint_and_saved_quote_lead(self):
        from .models import Lead

        body = {"package_id": str(self.pkg.id), "mode": "single", "travellers": [{}],
                "rooms": [{"stay_type": "airbnb_1bed", "occupants": [0]}]}
        res = self.client.post("/api/bookings/quote/", body, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], "1999.00")

        from unittest import mock
        with mock.patch("payments.email.deliver") as deliver:
            res = self.client.post("/api/leads/", {
                "name": "Kofi", "email": "kofi@example.com", "source": "saved_quote",
                "quote": {"total": "1.00"}, "selection": body,
            }, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        lead = Lead.objects.get()
        self.assertEqual(lead.quote["total"], "1999.00")  # server-priced, client value ignored
        self.assertIsNotNone(lead.reply_due_at)
        kind, to, _subject, html_body = deliver.call_args.args
        self.assertEqual((kind, to), ("saved_quote", "kofi@example.com"))
        self.assertIn("1,999.00", html_body)

    def test_hold_and_checkout_endpoints(self):
        res = self.client.post("/api/bookings/holds/", {"package_id": str(self.pkg.id), "stay_types": ["fourpoints_couple"]},
                               content_type="application/json")
        self.assertEqual(res.status_code, 201)
        token = res.json()["token"]
        res = self.client.post("/api/bookings/checkout/component/", {
            "package_id": str(self.pkg.id), "mode": "couple", "hold_token": token,
            "travellers": [{"name": "Ama Owusu"}, {"name": "Kojo Owusu"}],
            "rooms": [{"stay_type": "fourpoints_couple", "occupants": [0, 1]}],
            "first_name": "Ama", "last_name": "Owusu", "email": "ama@example.com",
            "expected_total": "3318.00",
        }, content_type="application/json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["total_amount"], "3318.00")


class AdminScreensTests(TestCase):
    """Phase 4 admin: screens render and staff actions keep the booking rules."""

    # Same catalogue fixture and checkout helper as the pricing tests, without re-running them.
    setUpTestData = ComponentPricingTests.__dict__["setUpTestData"]
    checkout = ComponentPricingTests.checkout

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.staff = get_user_model().objects.create_superuser(
            email="ops@example.com", password="x-Strong-pass-1", first_name="Akosua", last_name="Darko")
        self.client.force_login(self.staff)

    def booked(self, n=2, stay="airbnb_1bed"):
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, [stay])
        return self.checkout(token, n=n, rooms=[{"stay_type": stay, "occupants": list(range(n))}])

    def test_screens_render(self):
        booking = self.booked()
        urls = [
            "/admin/", "/admin/reports/", "/admin/reports/?export=csv",
            "/admin/bookings/booking/", "/admin/bookings/booking/?queue=needs_coordinator",
            "/admin/bookings/booking/?queue=contact_overdue", f"/admin/bookings/booking/{booking.pk}/change/",
            "/admin/bookings/lead/", "/admin/packages/roomunit/", "/admin/packages/roomunit/?state=held",
            f"/admin/packages/travelpackage/{self.pkg.pk}/change/", "/admin/payments/payment/", "/admin/payments/opsconfig/1/change/",
        ]
        from payments.models import OpsConfig
        OpsConfig.get()
        for url in urls:
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_assign_coordinator_records_time(self):
        booking = self.booked()
        self.client.post("/admin/bookings/booking/", {
            "action": "assign_coordinator", "_selected_action": [booking.pk], "apply": "1", "coordinator": self.staff.pk})
        booking.refresh_from_db()
        self.assertEqual(booking.coordinator, self.staff)
        self.assertIsNotNone(booking.coordinator_assigned_at)

    def test_quote_reminder_needs_marketing_consent(self):
        from unittest import mock
        from .models import Lead
        lead = Lead.objects.create(name="K", email="k@example.com", quote={"total": "1.00"}, marketing_consent=False)
        with mock.patch("payments.email.send_saved_quote") as send:
            self.client.get(f"/admin/bookings/lead/{lead.pk}/send-reminder/")
            send.assert_not_called()
            Lead.objects.filter(pk=lead.pk).update(marketing_consent=True)
            self.client.get(f"/admin/bookings/lead/{lead.pk}/send-reminder/")
            send.assert_called_once()

    def test_move_allocation_keeps_guests_with_their_booking(self):
        from packages.models import RoomUnit
        from .services import reassign_unit
        booking = self.booked()
        unit = booking.room_units.get()
        target = RoomUnit.objects.available().filter(stay_type=unit.stay_type).exclude(pk=unit.pk).first()
        reassign_unit(unit, target)
        self.assertEqual(set(booking.travellers.values_list("unit", flat=True)), {target.pk})
        unit.refresh_from_db()
        self.assertIsNone(unit.booking_id)

    def test_publish_from_package_page(self):
        from packages.models import PriceListVersion
        self.client.get(f"/admin/packages/travelpackage/{self.pkg.pk}/publish-prices/")
        self.assertEqual(PriceListVersion.objects.get(package=self.pkg).number, 1)


@override_settings(WHATSAPP_APP_SECRET="app-secret", WHATSAPP_VERIFY_TOKEN="verify-me",
                   WHATSAPP_PHONE_NUMBER_ID="123", WHATSAPP_TOKEN="tok")
class AssistantTests(TestCase):
    """Chatbot + WhatsApp hand-off (Change Request §8)."""

    @classmethod
    def setUpTestData(cls):
        from packages.models import PackageFAQ

        cls.pkg = make_package(is_featured=True)
        PackageOption.objects.create(package=cls.pkg, hotel_name="Four Points", occupancy="single",
                                     price_per_person=Decimal("899.00"))
        PackageFAQ.objects.create(package=cls.pkg, question="When should I arrive?", answer="By Wednesday.")
        make_package(title="Hidden Tour", is_active=False)

    def post_webhook(self, payload, secret=b"app-secret"):
        import hashlib, hmac, json
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        return self.client.generic("POST", "/api/whatsapp/webhook/", body, content_type="application/json",
                                   HTTP_X_HUB_SIGNATURE_256=sig)

    def message(self, msg_id, text="Hi"):
        return {"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "233200000000", "profile": {"name": "Ama"}}],
            "messages": [{"from": "233200000000", "id": msg_id, "type": "text", "text": {"body": text}}],
        }}]}]}

    def test_prompt_carries_live_public_facts_only(self):
        from .assistant import system_prompt
        prompt = system_prompt()
        self.assertIn("Flagship Tour", prompt)
        self.assertIn("899.00", prompt)
        self.assertIn("By Wednesday.", prompt)
        self.assertIn(f"/tour/{self.pkg.id}", prompt)
        self.assertNotIn("Hidden Tour", prompt)
        self.assertNotIn("server_now", prompt)
        self.assertNotIn("exchange_rate", prompt)

    def test_webhook_verification_and_signature(self):
        res = self.client.get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                         "hub.challenge": "42"})
        self.assertEqual((res.status_code, res.content), (200, b"42"))
        res = self.client.get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "wrong"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.post_webhook(self.message("m1"), secret=b"forged").status_code, 403)

    def test_reply_dedupe_handoff_and_pause(self):
        from unittest import mock
        from .models import ChatThread, Lead

        inline = mock.patch("bookings.whatsapp.in_background", lambda fn, *a: fn(*a))
        with inline, mock.patch("bookings.whatsapp.send_text") as send, \
                mock.patch("bookings.whatsapp.reply", return_value=("Hello!", None)):
            self.assertEqual(self.post_webhook(self.message("m1")).status_code, 200)
            self.post_webhook(self.message("m1"))  # Meta retry: answered once
        send.assert_called_once_with("233200000000", "Hello!")

        with inline, mock.patch("bookings.whatsapp.send_text"), \
                mock.patch("bookings.whatsapp.reply", return_value=("Passing you on.", "Refund question")):
            self.post_webhook(self.message("m2", "I want a refund"))
        thread = ChatThread.objects.get()
        self.assertTrue(thread.bot_paused)
        lead = Lead.objects.get()
        self.assertEqual((lead.source, lead.phone, lead.name, lead.email), ("whatsapp", "+233200000000", "Ama", ""))
        self.assertIn("Refund question", lead.message)
        self.assertIn("Guest: I want a refund", lead.message)

        with inline, mock.patch("bookings.whatsapp.send_text") as send, mock.patch("bookings.whatsapp.reply") as rep:
            self.post_webhook(self.message("m3", "Hello?"))
        rep.assert_not_called()  # staff own the thread now
        send.assert_not_called()
        self.assertEqual(ChatThread.objects.get().messages[-1]["content"], "Hello?")

    def test_site_chat_endpoint(self):
        from unittest import mock
        with mock.patch("bookings.assistant.reply", return_value=("Hi there", None)) as rep:
            res = self.client.post("/api/assistant/", {"messages": [{"role": "user", "content": "Dates?"}]},
                                   content_type="application/json")
        self.assertEqual(res.json(), {"reply": "Hi there", "handoff": None})
        rep.assert_called_once_with([{"role": "user", "content": "Dates?"}])
        res = self.client.post("/api/assistant/", {"messages": [{"role": "assistant", "content": "x"}]},
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_site_lead_form_still_requires_email(self):
        res = self.client.post("/api/leads/", {"name": "Kofi", "source": "chat", "message": "Hi"},
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.json())

    def test_assistant_failure_falls_back_to_a_person(self):
        from unittest import mock
        from .assistant import reply
        with override_settings(DEEPSEEK_API_KEY=""):
            text, handoff = reply([{"role": "user", "content": "Hi"}])
        self.assertIn("passing you to our team", text)
        self.assertIsNotNone(handoff)

    @override_settings(DEEPSEEK_API_KEY="k")
    def test_reply_parses_text_and_handoff_tool_call(self):
        from unittest import mock
        from .assistant import reply

        def api(content, tool_calls=None):
            res = mock.Mock()
            res.json.return_value = {"choices": [{"finish_reason": "stop",
                                                  "message": {"content": content, "tool_calls": tool_calls}}]}
            return res

        with mock.patch("bookings.assistant.requests.post", return_value=api("The trip is 6-11 January.")) as post:
            self.assertEqual(reply([{"role": "user", "content": "Dates?"}]), ("The trip is 6-11 January.", None))
        sent = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sent[1:], [{"role": "user", "content": "Dates?"}])

        call = {"id": "c1", "type": "function",
                "function": {"name": "hand_off_to_human", "arguments": '{"summary": "Refund for AZ-1"}'}}
        with mock.patch("bookings.assistant.requests.post", return_value=api(None, [call])):
            text, handoff = reply([{"role": "user", "content": "Refund please"}])
        self.assertEqual(handoff, "Refund for AZ-1")
        self.assertIn("passing this to our team", text)

    def test_knowledge_includes_programme_and_published_policies(self):
        from django.utils import timezone
        from packages.models import Itinerary
        from .assistant import system_prompt
        from .models import PolicyDocument
        Itinerary.objects.create(package=self.pkg, day=1, title="Wed 6 Jan", description="Welcome dinner with Michael.")
        PolicyDocument.objects.create(type="refund", version="1.0", title="Refund Policy", body="50% refund up to 60 days.",
                                      is_current=True, published_at=timezone.now())
        PolicyDocument.objects.create(type="terms", version="0.9", title="Draft Terms", body="Unpublished draft.")
        prompt = system_prompt()
        self.assertIn("Welcome dinner with Michael.", prompt)
        self.assertIn("50% refund up to 60 days.", prompt)
        self.assertNotIn("Unpublished draft.", prompt)

    def test_knowledge_explains_how_booking_works_from_live_settings(self):
        import json
        from payments.models import OpsConfig
        from .assistant import facts_json
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={"hold_minutes": 20})
        rules = json.loads(facts_json())["how_booking_works"]
        self.assertIn("MTN Mobile Money", rules["payment_methods"])
        self.assertIn("Card (Visa / Mastercard)", rules["payment_methods"])
        self.assertIn("20 minutes", rules["rooms_during_checkout"])
        self.assertIn("7 days", rules["adding_experiences_later"])
        self.assertIn("14 days", rules["name_changes_and_replacements"])
        # Switched-off features are not mentioned at all.
        self.assertNotIn("whatsapp_updates", rules)
        self.assertNotIn("academy_donations", rules)
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={
            "whatsapp_confirmation_template": "payment_received", "donations_enabled": True, "donation_subaccount": "ACCT_x"})
        rules = json.loads(facts_json())["how_booking_works"]
        self.assertIn("whatsapp_updates", rules)
        self.assertIn("academy_donations", rules)

    def test_fact_check_catches_anything_not_in_the_facts(self):
        from payments.models import OpsConfig
        from .assistant import facts_json, unsupported
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={
            "contact_email": "hello@azuratravels.live", "whatsapp_number": "+233 20 111 2222"})
        facts = facts_json() + ' "Refund: 50% up to 60 days. Call +233 24 123 4567 (sample)."'
        with override_settings(FRONTEND_URL="https://azuratravels.live"):
            ok = (f"The Core ticket is USD 899.00, or $899. 50% back up to 60 days. Book at "
                  f"https://azuratravels.live/tour/{self.pkg.id} or WhatsApp https://wa.me/233201112222. "
                  "Email hello@azuratravels.live or message +233 20 111 2222. Trip runs 6-11 January 2027.")
            self.assertEqual(unsupported(ok, facts), [])
            bad = ("For two of you it's $1,798, with 30% off. See https://example.com/deal, "
                   "email info@azura.com or call +233 24 123 4567.")
            self.assertEqual(unsupported(bad, facts), [
                "link https://example.com/deal", "email info@azura.com", "price 1,798", "30%", "phone +233 24 123 4567"])
            # IDs inside site links are never mistaken for phone numbers or prices.
            self.assertEqual(unsupported("See https://azuratravels.live/pay/12345678-1234-5678-9012-345678901234.", facts), [])

    @override_settings(DEEPSEEK_API_KEY="k")
    def test_reply_with_invented_facts_is_held_back_and_handed_off(self):
        from unittest import mock
        from .assistant import reply
        res = mock.Mock()
        res.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": "Sure! Two guests come to $1,500 with our 20% couples discount.", "tool_calls": None}}]}
        with mock.patch("bookings.assistant.requests.post", return_value=res) as post:
            text, handoff = reply([{"role": "user", "content": "How much for two?"}])
        self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0)
        self.assertNotIn("1,500", text)
        self.assertIn("I don't want to give you a wrong answer", text)
        self.assertIn("passing this to our team", text)
        self.assertIn("How much for two?", handoff)
