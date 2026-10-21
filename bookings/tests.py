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

class ComponentPricingTests(TestCase):
    """The client's acceptance totals and room/ticket rules (Change Request v2 §6)."""

    @classmethod
    def setUpTestData(cls):
        from packages.models import PackageAddon, PackageDiscountRule, RoomUnit, StayType

        cls.pkg = make_package(
            pricing_model=TravelPackage.PricingModel.COMPONENT, base_price_per_person=Decimal("899.00"),
            available_from=date(2027, 1, 6), available_to=date(2027, 1, 11),
        )

        def stay(code, cap, price, pct, units, min_occ=1, on_sale=True):
            st = StayType.objects.create(
                package=cls.pkg, code=code, name=code, kind="airbnb" if "airbnb" in code else "hotel",
                capacity=cap, min_occupancy=min_occ, price_per_night=Decimal(price),
                discount_percent=Decimal(pct), on_sale=on_sale,
            )
            for i in range(units):
                RoomUnit.objects.create(stay_type=st, label=f"{code}-{i}")
            return st

        stay("airbnb_1bed", 2, "220", "3", 2)
        stay("airbnb_2bed", 4, "340", "3", 1)
        stay("fp_standard", 1, "330", "5", 2)
        stay("fp_couple", 2, "380", "5", 1, min_occ=2)
        stay("fp_closed", 2, "999", "5", 1, on_sale=False)

        def addon(code, price):
            return PackageAddon.objects.create(package=cls.pkg, code=code, name=code, price=Decimal(price))

        members = [addon("enzo_vip", "220"), addon("waterfall_massage", "100"), addon("farewell_dinner", "100")]
        rule = PackageDiscountRule.objects.create(
            package=cls.pkg, name="Complete extras bundle", percent=Decimal("5"),
            scope=PackageDiscountRule.Scope.TRAVELLER_BUNDLE,
        )
        rule.required_addons.set(members)

    def quote(self, mode, travellers, rooms=(), plan="full"):
        from .pricing import compute_component_quote
        if isinstance(travellers, int):
            travellers = [{} for _ in range(travellers)]
        return compute_component_quote(self.pkg, mode=mode, travellers=travellers, rooms=list(rooms), payment_plan=plan)

    def assertRejected(self, *args, **kwargs):
        from .pricing import QuoteError
        with self.assertRaises(QuoteError):
            self.quote(*args, **kwargs)

    BUNDLE = ["enzo_vip", "waterfall_massage", "farewell_dinner"]

    def test_acceptance_totals(self):
        cases = [
            ("1 Core + 1-bed", "single", 1, [("airbnb_1bed", [0])], "1725.63"),
            ("2 Core + same 1-bed", "couple", 2, [("airbnb_1bed", [0, 1])], "2597.66"),
            ("4 Core + one 2-bed", "group", 4, [("airbnb_2bed", [0, 1, 2, 3])], "4807.32"),
            ("1 Core + Four Points", "single", 1, [("fp_standard", [0])], "2108.05"),
            ("Couple + Four Points couple", "couple", 2, [("fp_couple", [0, 1])], "3152.10"),
        ]
        for label, mode, n, rooms, expected in cases:
            with self.subTest(label):
                q = self.quote(mode, n, [{"stay_type": s, "occupants": o} for s, o in rooms])
                self.assertEqual(q.total, Decimal(expected))

    def test_experience_totals(self):
        cases = [
            ("Two-person ENZO", [["enzo_vip"], ["enzo_vip"]], "440.00"),
            ("Two complete bundles", [self.BUNDLE, self.BUNDLE], "798.00"),
            ("One bundle + one massage", [self.BUNDLE, ["waterfall_massage"]], "499.00"),
        ]
        for label, picks, expected in cases:
            with self.subTest(label):
                q = self.quote("couple", [{"experiences": p} for p in picks])
                self.assertEqual(q.total - q.core_total, Decimal(expected))

    def test_core_only_has_no_discount(self):
        self.assertEqual(self.quote("couple", 2).total, Decimal("1798.00"))

    def test_one_guest_may_book_a_two_bed_unit(self):
        q = self.quote("single", 1, [{"stay_type": "airbnb_2bed", "occupants": [0]}])
        self.assertEqual(q.total, Decimal("2191.23"))  # (899 + 1360) × 0.97

    def test_room_rules(self):
        self.assertRejected("group", 5, [{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2, 3, 4]}])
        self.assertRejected("group", 3, [{"stay_type": "airbnb_1bed", "occupants": [0, 1, 2]}])
        # Every occupant must be a traveller (= hold Core); nobody in two rooms.
        self.assertRejected("couple", 2, [{"stay_type": "airbnb_1bed", "occupants": [0, 2]}])
        self.assertRejected("group", 3, [{"stay_type": "airbnb_1bed", "occupants": [0, 1]},
                                         {"stay_type": "fp_standard", "occupants": [1]}])
        # Couple: one shared room; standard rooms stay single; couple room needs two.
        self.assertRejected("couple", 2, [{"stay_type": "fp_standard", "occupants": [0, 1]}])
        self.assertRejected("couple", 2, [{"stay_type": "fp_standard", "occupants": [0]},
                                          {"stay_type": "fp_standard", "occupants": [1]}])
        self.assertRejected("group", 1, [{"stay_type": "fp_couple", "occupants": [0]}])
        self.assertRejected("single", 1, [{"stay_type": "fp_closed", "occupants": [0]}])
        self.assertRejected("single", 2)
        self.assertRejected("couple", 3)

    def test_quote_rejects_sold_out_rooms(self):
        self.assertRejected("group", 2, [{"stay_type": "airbnb_2bed", "occupants": [0]},
                                         {"stay_type": "airbnb_2bed", "occupants": [1]}])

    def test_deposit_threshold(self):
        TravelPackage.objects.filter(pk=self.pkg.pk).update(
            allow_installments=True, deposit_minimum=Decimal("1000"), deposit_threshold=Decimal("1000"))
        self.pkg.refresh_from_db()
        self.assertRejected("single", 1, plan="installment")  # $899 < $1,000 → pay in full
        q = self.quote("couple", 2, plan="installment")
        self.assertEqual(q.amount_due_today, Decimal("1000.00"))

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

    def test_checkout_issues_one_core_ticket_per_traveller(self):
        from .services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token)
        self.assertEqual(booking.total_amount, Decimal("2597.66"))
        self.assertEqual(booking.travellers.count(), 2)
        unit = booking.room_units.get()
        self.assertEqual(set(booking.travellers.values_list("unit", flat=True)), {unit.pk})
        self.assertIsNotNone(unit.held_until)  # unpaid window, not yet sold

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

        token, _, _ = create_hold(self.pkg, ["airbnb_1bed", "fp_standard"])
        booking = self.checkout(token, mode="group", n=3, rooms=[
            {"stay_type": "airbnb_1bed", "occupants": [0, 1]}, {"stay_type": "fp_standard", "occupants": [2]},
        ])
        hotel = booking.room_units.get(stay_type__code="fp_standard")
        g0, g1, g2 = booking.travellers.order_by("created_at", "full_name")
        with self.assertRaises(IllegalTransition):
            move_traveller(g0, hotel)  # standard room already full
        move_traveller(g2, None)
        move_traveller(g0, hotel)
        self.assertEqual(booking.travellers.count(), 3)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("4705.71"))  # 2597.66 + 2108.05, not re-priced

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

    # ── API ───────────────────────────────────────────────────────────────────

    def test_quote_endpoint_and_saved_quote_lead(self):
        from .models import Lead

        body = {"package_id": str(self.pkg.id), "mode": "single", "travellers": [{}],
                "rooms": [{"stay_type": "airbnb_1bed", "occupants": [0]}]}
        res = self.client.post("/api/bookings/quote/", body, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], "1725.63")

        res = self.client.post("/api/leads/", {
            "name": "Kofi", "email": "kofi@example.com", "source": "saved_quote",
            "quote": {"total": "1.00"}, "selection": body,
        }, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        lead = Lead.objects.get()
        self.assertEqual(lead.quote["total"], "1725.63")  # server-priced, client value ignored
        self.assertIsNotNone(lead.reply_due_at)

    def test_hold_and_checkout_endpoints(self):
        res = self.client.post("/api/bookings/holds/", {"package_id": str(self.pkg.id), "stay_types": ["fp_couple"]},
                               content_type="application/json")
        self.assertEqual(res.status_code, 201)
        token = res.json()["token"]
        res = self.client.post("/api/bookings/checkout/component/", {
            "package_id": str(self.pkg.id), "mode": "couple", "hold_token": token,
            "travellers": [{"name": "Ama Owusu"}, {"name": "Kojo Owusu"}],
            "rooms": [{"stay_type": "fp_couple", "occupants": [0, 1]}],
            "first_name": "Ama", "last_name": "Owusu", "email": "ama@example.com",
            "expected_total": "3152.10",
        }, content_type="application/json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["total_amount"], "3152.10")
