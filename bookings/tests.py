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
