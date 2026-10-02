"""
Tests for the money foundation: the payment ledger, apply_successful_payment
guarantees, and the initialize endpoint's fresh-reference behavior.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import Booking
from packages.models import TravelPackage
from .models import Payment
from .money import quantize, to_subunits
from .services import (
    apply_successful_payment,
    generate_payment_reference,
    mark_payment_unsuccessful,
    record_offline_payment,
)


def make_booking(total="1200.00", status=Booking.Status.PENDING):
    package = TravelPackage.objects.create(
        title="Test Tour",
        slug=f"test-tour-{TravelPackage.objects.count()}",
        category=TravelPackage.Category.CULTURAL,
        description="x",
        duration_days=3,
        price_shared=Decimal("600.00"),
        currency="GHS",
    )
    return Booking.objects.create(
        package=package,
        first_name="Kwame",
        last_name="Mensah",
        email="kwame@example.com",
        num_guests=2,
        travel_date="2027-01-04",
        unit_price=Decimal("600.00"),
        total_amount=Decimal(total),
        currency="GHS",
        status=status,
    )


def make_payment(booking, amount=None, **kwargs):
    return Payment.objects.create(
        booking=booking,
        paystack_reference=generate_payment_reference(booking),
        amount=quantize(amount if amount is not None else booking.balance),
        currency=booking.currency,
        **kwargs,
    )


def gateway_ok(payment):
    return {"amount": to_subunits(payment.amount), "currency": payment.currency, "status": "success"}


class MoneyHelpersTests(TestCase):
    def test_quantize_rounds_half_up(self):
        self.assertEqual(quantize(Decimal("100.005")), Decimal("100.01"))
        self.assertEqual(quantize("100.004"), Decimal("100.00"))

    def test_to_subunits_never_truncates(self):
        self.assertEqual(to_subunits(Decimal("100.999")), 10100)
        self.assertEqual(to_subunits(Decimal("1200.00")), 120000)


@patch("payments.email.send_booking_confirmation")
class ApplyPaymentTests(TestCase):
    def test_success_confirms_booking_and_caches_total(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        result = apply_successful_payment(payment, gateway_ok(payment))

        booking.refresh_from_db()
        self.assertTrue(result.applied)
        self.assertTrue(result.promoted)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.amount_paid, Decimal("1200.00"))
        self.assertEqual(booking.balance, Decimal("0.00"))
        self.assertTrue(booking.is_paid)

    def test_idempotent_under_verify_webhook_race(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        apply_successful_payment(payment, gateway_ok(payment))
        result = apply_successful_payment(payment, gateway_ok(payment))

        booking.refresh_from_db()
        self.assertTrue(result.already_applied)
        self.assertEqual(booking.amount_paid, Decimal("1200.00"))  # not double-credited

    def test_amount_mismatch_never_confirms(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        bad = {"amount": 5000, "currency": "GHS", "status": "success"}
        result = apply_successful_payment(payment, bad)

        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertIn("gateway_mismatch", result.problems)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertTrue(payment.needs_review)
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_currency_mismatch_never_confirms(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        bad = {"amount": to_subunits(payment.amount), "currency": "USD", "status": "success"}
        result = apply_successful_payment(payment, bad)

        self.assertIn("gateway_mismatch", result.problems)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_payment_against_cancelled_booking_flagged_not_applied(self, mock_email):
        booking = make_booking(status=Booking.Status.CANCELLED)
        payment = make_payment(booking, amount="1200.00")
        result = apply_successful_payment(payment, gateway_ok(payment))

        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertIn("terminal_booking", result.problems)
        self.assertEqual(payment.status, Payment.Status.SUCCESS)  # money is recorded…
        self.assertTrue(payment.needs_review)                     # …and flagged
        self.assertEqual(booking.status, Booking.Status.CANCELLED)  # never revived
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_partial_payment_leaves_booking_pending_until_threshold(self, mock_email):
        booking = make_booking(total="4600.00")
        p1 = make_payment(booking, amount="1000.00")
        apply_successful_payment(p1, gateway_ok(p1))

        booking.refresh_from_db()
        # No installment plan fields yet (phase 2): threshold is the total.
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("1000.00"))
        self.assertEqual(booking.balance, Decimal("3600.00"))

        p2 = make_payment(booking, amount="3600.00")
        result = apply_successful_payment(p2, gateway_ok(p2))
        booking.refresh_from_db()
        self.assertTrue(result.promoted)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertTrue(booking.is_paid)

    def test_overpayment_recorded_and_flagged(self, mock_email):
        booking = make_booking(total="1200.00")
        p1 = make_payment(booking, amount="1200.00")
        p2 = make_payment(booking, amount="1200.00")  # concurrent duplicate
        apply_successful_payment(p1, gateway_ok(p1))
        result = apply_successful_payment(p2, gateway_ok(p2))

        booking.refresh_from_db()
        p2.refresh_from_db()
        self.assertIn("overpaid", result.problems)
        self.assertTrue(p2.needs_review)
        self.assertEqual(booking.amount_paid, Decimal("2400.00"))  # ledger truth kept

    def test_confirmation_email_sent_once_on_promotion_only(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        with self.captureOnCommitCallbacks(execute=True):
            apply_successful_payment(payment, gateway_ok(payment))
        with self.captureOnCommitCallbacks(execute=True):
            apply_successful_payment(payment, gateway_ok(payment))  # idempotent re-verify
        self.assertEqual(mock_email.call_count, 1)

    def test_failed_never_downgrades_success(self, mock_email):
        booking = make_booking()
        payment = make_payment(booking)
        apply_successful_payment(payment, gateway_ok(payment))
        mark_payment_unsuccessful(payment, "failed", {"status": "failed"})
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)

    def test_offline_payment_runs_same_path(self, mock_email):
        booking = make_booking(total="1200.00")
        result = record_offline_payment(
            booking, amount="1200.00", method=Payment.Method.BANK_TRANSFER, note="Wire ref 123",
        )
        booking.refresh_from_db()
        self.assertTrue(result.promoted)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.payments.get().method, Payment.Method.BANK_TRANSFER)


class LedgerImmutabilityTests(TestCase):
    def test_successful_payment_amount_is_frozen(self):
        booking = make_booking()
        payment = make_payment(booking)
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(payment, gateway_ok(payment))
        payment.refresh_from_db()
        payment.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            payment.save()


class InitializeEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("payments.views.initialize_transaction")
    def test_each_call_mints_fresh_reference_and_abandons_prior_pending(self, mock_init):
        mock_init.return_value = {"access_code": "abc", "authorization_url": "https://pay/x"}
        booking = make_booking()

        r1 = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        r2 = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.data["reference"], r2.data["reference"])

        statuses = list(booking.payments.order_by("created_at").values_list("status", flat=True))
        self.assertEqual(statuses, [Payment.Status.ABANDONED, Payment.Status.PENDING])

    @patch("payments.views.initialize_transaction")
    def test_initialize_refuses_cancelled_and_fully_paid(self, mock_init):
        mock_init.return_value = {"access_code": "abc", "authorization_url": "https://pay/x"}

        cancelled = make_booking(status=Booking.Status.CANCELLED)
        r = self.client.post("/api/payments/initialize/", {"booking_id": str(cancelled.id)}, format="json")
        self.assertEqual(r.status_code, 400)

        paid = make_booking()
        payment = make_payment(paid)
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(payment, gateway_ok(payment))
        r = self.client.post("/api/payments/initialize/", {"booking_id": str(paid.id)}, format="json")
        self.assertEqual(r.status_code, 400)

    @patch("payments.views.initialize_transaction")
    def test_amount_is_outstanding_balance(self, mock_init):
        mock_init.return_value = {"access_code": "abc", "authorization_url": "https://pay/x"}
        booking = make_booking(total="4600.00")
        p1 = make_payment(booking, amount="1000.00")
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(p1, gateway_ok(p1))

        r = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        self.assertEqual(r.data["amount"], "3600.00")


class StatusEndpointTests(TestCase):
    def test_status_is_db_only(self):
        booking = make_booking()
        payment = make_payment(booking)
        with patch("payments.paystack.verify_transaction") as mock_verify:
            r = self.client.get(f"/api/payments/status/{payment.paystack_reference}/")
            mock_verify.assert_not_called()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(r.data["balance"], "1200.00")


# ── Phase 4: refund computation, allocation, processing ──────────────────────

from datetime import date, timedelta

from django.utils import timezone

from accounts.models import User
from .models import Refund
from .refunds import compute_refund, create_pending_refunds, mark_refund_processed


REFUND_TIERS = [
    {"min_days": 60, "percent": 90},
    {"min_days": 30, "percent": 60},
    {"min_days": 14, "percent": 40},
    {"min_days": 0, "percent": 0},
]


def make_refundable_booking(paid_amounts, *, days_to_departure=90, addons=None, total="4700.00"):
    """Confirmed booking with the given successful payments on the ledger."""
    booking = make_booking(total=total, status=Booking.Status.CONFIRMED)
    booking.travel_date = timezone.now().date() + timedelta(days=days_to_departure)
    booking.refund_tiers_snapshot = REFUND_TIERS
    booking.addons = addons or []
    booking.save()
    with patch("payments.email.send_booking_confirmation"):
        for amount in paid_amounts:
            p = make_payment(booking, amount=amount)
            apply_successful_payment(p, gateway_ok(p))
    booking.refresh_from_db()
    return booking


class ComputeRefundTests(TestCase):
    def test_tier_table(self):
        """The policy table: 60+ → 90%, 30–59 → 60%, 14–29 → 40%, <14 → 0%."""
        for days, expected in [(90, "900.00"), (45, "600.00"), (20, "400.00"), (5, "0.00"), (60, "900.00"), (30, "600.00"), (14, "400.00")]:
            booking = make_refundable_booking(["1000.00"], days_to_departure=days)
            computed = compute_refund(booking)
            self.assertEqual(
                str(computed["refund_total"]), expected,
                f"days={days}: expected {expected}, got {computed['refund_total']}",
            )

    def test_non_refundable_addons_excluded_before_percentage(self):
        booking = make_refundable_booking(
            ["1300.00"],
            days_to_departure=90,
            addons=[{"code": "visa", "name": "Visa on Arrival", "unit_price": "150.00",
                     "quantity": 2, "line_total": "300.00", "refundable": False}],
        )
        computed = compute_refund(booking)
        # (1300 - 300 non-refundable) × 90% = 900
        self.assertEqual(computed["refund_total"], Decimal("900.00"))
        self.assertEqual(computed["breakdown"]["non_refundable_components"], "300.00")

    def test_allocation_spans_multiple_payments_newest_first(self):
        booking = make_refundable_booking(["1000.00", "2000.00", "1700.00"], days_to_departure=90)
        computed = compute_refund(booking)
        # net paid 4700 × 90% = 4230 → 1700 (newest) + 2000 + 530
        self.assertEqual(computed["refund_total"], Decimal("4230.00"))
        amounts = [leg["amount"] for leg in computed["allocation"]]
        self.assertEqual(amounts, [Decimal("1700.00"), Decimal("2000.00"), Decimal("530.00")])
        self.assertEqual(computed["breakdown"]["unallocatable"], "0.00")

    def test_prior_refunds_reduce_the_base(self):
        booking = make_refundable_booking(["2000.00"], days_to_departure=90)
        legs = create_pending_refunds(booking, reason="partial goodwill")
        # Manually process only part: replace computed legs with a smaller processed one
        for leg in legs:
            Refund.objects.filter(pk=leg.pk).update(status=Refund.Status.REJECTED)
        user = User.objects.create_user(email="ops@azura.com", first_name="Ops", last_name="Team")
        partial = Refund.objects.create(
            booking=booking, payment=booking.payments.get(),
            amount=Decimal("500.00"), currency=booking.currency,
        )
        mark_refund_processed(partial, by_user=user)

        booking.refresh_from_db()
        self.assertEqual(booking.amount_refunded, Decimal("500.00"))
        computed = compute_refund(booking)
        # net paid 1500 × 90% = 1350
        self.assertEqual(computed["refund_total"], Decimal("1350.00"))


class RefundLifecycleTests(TestCase):
    def test_cancel_booking_creates_pending_legs_and_double_cancel_fails(self):
        from bookings.services import IllegalTransition, cancel_booking

        booking = make_refundable_booking(["1000.00"], days_to_departure=90)
        cancel_booking(booking, reason=Booking.CancellationReason.CUSTOMER)

        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.cancellation_reason, "customer")
        self.assertEqual(booking.refunds.filter(status=Refund.Status.PENDING).count(), 1)
        self.assertEqual(booking.refunds.get().amount, Decimal("900.00"))

        with self.assertRaises(IllegalTransition):
            cancel_booking(booking, reason=Booking.CancellationReason.ADMIN)

    def test_double_refund_creation_refused(self):
        booking = make_refundable_booking(["1000.00"], days_to_departure=90)
        create_pending_refunds(booking, reason="x")
        with self.assertRaises(ValueError):
            create_pending_refunds(booking, reason="double click")

    def test_mark_processed_is_idempotent_and_updates_cache(self):
        booking = make_refundable_booking(["1000.00"], days_to_departure=90)
        (leg,) = create_pending_refunds(booking, reason="x")
        user = User.objects.create_user(email="ops2@azura.com", first_name="Ops", last_name="Team")

        mark_refund_processed(leg, by_user=user, external_reference="PSK-REF-1")
        mark_refund_processed(leg, by_user=user)  # idempotent

        booking.refresh_from_db()
        self.assertEqual(booking.amount_refunded, Decimal("900.00"))
        self.assertEqual(booking.refunds.filter(status=Refund.Status.PROCESSED).count(), 1)

    def test_fully_refunded_payment_marked_on_ledger(self):
        booking = make_refundable_booking(["1000.00"], days_to_departure=90, total="1000.00")
        payment = booking.payments.get()
        user = User.objects.create_user(email="ops3@azura.com", first_name="Ops", last_name="Team")
        leg = Refund.objects.create(
            booking=booking, payment=payment,
            amount=Decimal("1000.00"), currency=booking.currency,
        )
        mark_refund_processed(leg, by_user=user)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUNDED)

    def test_no_payment_no_refund_legs_on_cancel(self):
        from bookings.services import cancel_booking

        booking = make_booking()  # pending, nothing paid
        cancel_booking(booking, reason=Booking.CancellationReason.EXPIRED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.refunds.count(), 0)


# ── FX: USD-denominated ledger charged in GHS ────────────────────────────────

class FxChargeTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _usd_booking(self, rate="15.5000"):
        booking = make_booking(total="1000.00")
        booking.currency = "USD"
        booking.save()
        package = booking.package
        package.currency = "USD"
        package.fx_mode = "manual"  # no live fetch in tests
        package.charge_exchange_rate = Decimal(rate)
        package.save()
        return booking

    @patch("payments.views.initialize_transaction")
    def test_usd_booking_charged_in_ghs_at_package_rate(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._usd_booking()

        r = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["amount"], "1000.00")           # ledger: USD
        self.assertEqual(r.data["currency"], "USD")
        self.assertEqual(r.data["charged_amount"], "15500.00")  # gateway: GHS
        self.assertEqual(r.data["charged_currency"], "GHS")

        # The gateway was sent GHS subunits, not USD.
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 1550000)
        self.assertEqual(kwargs["currency"], "GHS")

        payment = booking.payments.get()
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertEqual(payment.charged_amount, Decimal("15500.00"))
        self.assertEqual(payment.exchange_rate, Decimal("15.5000"))

    @patch("payments.views.initialize_transaction")
    def test_missing_rate_refuses_cleanly(self, mock_init):
        booking = self._usd_booking()
        booking.package.charge_exchange_rate = None
        booking.package.save()

        r = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        self.assertEqual(r.status_code, 503)
        mock_init.assert_not_called()

    @patch("payments.views.initialize_transaction")
    def test_verification_checks_ghs_charge_and_credits_usd_ledger(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._usd_booking()
        self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        payment = booking.payments.get()

        # Gateway reports the GHS charge → applied; ledger credited in USD.
        with patch("payments.email.send_booking_confirmation"):
            result = apply_successful_payment(payment, {
                "amount": 1550000, "currency": "GHS", "status": "success",
            })
        booking.refresh_from_db()
        self.assertTrue(result.promoted)
        self.assertEqual(booking.amount_paid, Decimal("1000.00"))  # USD, exact
        self.assertEqual(booking.currency, "USD")

    @patch("payments.views.initialize_transaction")
    def test_verification_rejects_wrong_ghs_amount(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._usd_booking()
        self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        payment = booking.payments.get()

        # Ledger-USD subunits sent where GHS expected → mismatch, never applied.
        result = apply_successful_payment(payment, {
            "amount": 100000, "currency": "USD", "status": "success",
        })
        booking.refresh_from_db()
        self.assertIn("gateway_mismatch", result.problems)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    @patch("payments.views.initialize_transaction")
    def test_rate_update_applies_to_next_payment_only(self, mock_init):
        """Installments months apart: each charge converts at the CURRENT rate,
        but the USD ledger sums exactly regardless."""
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._usd_booking(rate="15.0000")

        self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "intent": "custom", "amount": "400.00",
        }, format="json")
        p1 = booking.payments.latest("created_at")
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(p1, {"amount": 600000, "currency": "GHS", "status": "success"})

        booking.package.charge_exchange_rate = Decimal("16.0000")
        booking.package.save()

        self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        p2 = booking.payments.filter(status=Payment.Status.PENDING).get()
        self.assertEqual(p2.amount, Decimal("600.00"))            # USD balance
        self.assertEqual(p2.charged_amount, Decimal("9600.00"))   # new rate
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(p2, {"amount": 960000, "currency": "GHS", "status": "success"})

        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("1000.00"))  # USD ledger exact
        self.assertTrue(booking.is_paid)


# ── Live FX service: provider, cache, fallback chain, margin ─────────────────

from django.core.cache import cache as django_cache

from packages.models import TravelPackage
from .fx import FxUnavailable, effective_charge_rate, get_market_rate
from .models import FxRate


def usd_package(**overrides):
    defaults = dict(
        title="FX Tour",
        slug=f"fx-tour-{TravelPackage.objects.count()}",
        category=TravelPackage.Category.LUXURY_TRAVEL,
        description="x",
        duration_days=10,
        currency="USD",
    )
    defaults.update(overrides)
    return TravelPackage.objects.create(**defaults)


def provider_ok(rate=15.5):
    response = type("R", (), {})()
    response.raise_for_status = lambda: None
    response.json = lambda: {"result": "success", "rates": {"GHS": rate}}
    return response


class FxServiceTests(TestCase):
    def setUp(self):
        django_cache.clear()

    @patch("payments.fx.requests.get")
    def test_live_fetch_caches_and_persists_history(self, mock_get):
        mock_get.return_value = provider_ok(15.5)

        first = get_market_rate("USD", "GHS")
        second = get_market_rate("USD", "GHS")  # served from cache

        self.assertEqual(first.rate, Decimal("15.500000"))
        self.assertEqual(second.source, "cache")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(FxRate.objects.count(), 1)  # audit row appended once

    @patch("payments.fx.requests.get")
    def test_provider_down_uses_recent_db_rate(self, mock_get):
        import requests as requests_lib
        mock_get.side_effect = requests_lib.ConnectionError("provider down")
        FxRate.objects.create(base="USD", quote="GHS", rate=Decimal("15.2"), source="test")

        resolved = get_market_rate("USD", "GHS")
        self.assertEqual(resolved.source, "db-fallback")
        self.assertEqual(resolved.rate, Decimal("15.2"))

    @patch("payments.fx.requests.get")
    def test_stale_db_rate_not_used_falls_to_manual(self, mock_get):
        import requests as requests_lib
        mock_get.side_effect = requests_lib.ConnectionError("provider down")
        stale = FxRate.objects.create(base="USD", quote="GHS", rate=Decimal("15.2"), source="test")
        FxRate.objects.filter(pk=stale.pk).update(
            fetched_at=timezone.now() - timedelta(hours=48)
        )
        package = usd_package(charge_exchange_rate=Decimal("15.0000"))

        rate, source = effective_charge_rate(package)
        self.assertEqual(source, "manual-fallback")
        self.assertEqual(rate, Decimal("15.0000"))

    @patch("payments.fx.requests.get")
    def test_nothing_available_raises(self, mock_get):
        import requests as requests_lib
        mock_get.side_effect = requests_lib.ConnectionError("provider down")
        package = usd_package()  # live mode, no manual fallback
        with self.assertRaises(FxUnavailable):
            effective_charge_rate(package)

    @patch("payments.fx.requests.get")
    def test_margin_applied_on_top_of_market_rate(self, mock_get):
        mock_get.return_value = provider_ok(15.5)
        package = usd_package(fx_margin_percent=Decimal("2.00"))

        rate, source = effective_charge_rate(package)
        self.assertEqual(rate, Decimal("15.810000"))  # 15.5 × 1.02
        self.assertEqual(source, "open.er-api.com")

    @patch("payments.fx.requests.get")
    def test_manual_mode_never_fetches(self, mock_get):
        package = usd_package(fx_mode="manual", charge_exchange_rate=Decimal("15.7500"))
        rate, source = effective_charge_rate(package)
        self.assertEqual(rate, Decimal("15.7500"))
        self.assertEqual(source, "manual")
        mock_get.assert_not_called()

    @patch("payments.fx.requests.get")
    def test_live_rate_flows_into_initialize_charge(self, mock_get):
        mock_get.return_value = provider_ok(15.5)
        booking = make_booking(total="1000.00")
        booking.currency = "USD"
        booking.save()
        package = booking.package
        package.currency = "USD"
        package.fx_margin_percent = Decimal("2.00")
        package.save()

        client = APIClient()
        with patch("payments.views.initialize_transaction") as mock_init:
            mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
            r = client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["charged_amount"], "15810.00")   # 1000 × 15.5 × 1.02
        self.assertEqual(r.data["exchange_rate"], "15.810000")
        payment = booking.payments.get()
        self.assertEqual(payment.exchange_rate, Decimal("15.810000"))




# ── FX hardening: sanity bounds, provider failover, refresh command ──────────

class FxHardeningTests(TestCase):
    def setUp(self):
        django_cache.clear()

    @patch("payments.fx.requests.get")
    def test_garbage_rate_rejected_by_sanity_check(self, mock_get):
        """A corrupted feed must never set prices: 15.5 → 1.55 (90% off) is
        rejected and the DB fallback serves instead."""
        FxRate.objects.create(base="USD", quote="GHS", rate=Decimal("15.5"), source="seed")
        mock_get.return_value = provider_ok(1.55)  # decimal-point bug upstream

        resolved = get_market_rate("USD", "GHS")
        self.assertEqual(resolved.source, "db-fallback")
        self.assertEqual(resolved.rate, Decimal("15.5"))
        # The garbage rate was never stored as truth.
        self.assertEqual(FxRate.objects.count(), 1)

    @patch("payments.fx.requests.get")
    def test_small_market_move_passes_sanity_check(self, mock_get):
        FxRate.objects.create(base="USD", quote="GHS", rate=Decimal("15.5"), source="seed")
        mock_get.return_value = provider_ok(16.0)  # ~3.2% move — normal

        resolved = get_market_rate("USD", "GHS")
        self.assertEqual(resolved.source, "open.er-api.com")
        self.assertEqual(resolved.rate, Decimal("16.000000"))

    @patch("payments.fx.requests.get")
    def test_second_provider_used_when_first_is_down(self, mock_get):
        import requests as requests_lib

        second = type("R", (), {})()
        second.raise_for_status = lambda: None
        second.json = lambda: {"date": "2026-08-14", "usd": {"ghs": 15.4}}

        mock_get.side_effect = [requests_lib.ConnectionError("primary down"), second]

        resolved = get_market_rate("USD", "GHS")
        self.assertEqual(resolved.source, "currency-api(jsdelivr)")
        self.assertEqual(resolved.rate, Decimal("15.400000"))

    @patch("payments.fx.requests.get")
    def test_refresh_command_warms_the_fallback(self, mock_get):
        from django.core.management import call_command
        from io import StringIO

        mock_get.return_value = provider_ok(15.5)
        usd_package()  # active USD package → USD needs refreshing

        out = StringIO()
        call_command("refresh_fx_rates", stdout=out)
        self.assertIn("1 USD = 15.500000 GHS", out.getvalue())
        self.assertEqual(FxRate.objects.filter(base="USD", quote="GHS").count(), 1)


# ── Full lifecycle E2E — real HTTP endpoints, genuine webhook signatures ─────

import hashlib
import hmac as hmac_lib
import json as json_lib

from django.test import override_settings

from bookings.models import PolicyDocument
from packages.models import PackageOption


@override_settings(PAYSTACK_SECRET_KEY="sk_test_e2e_secret")
class FullLifecycleE2ETests(TestCase):
    """
    The entire installment journey through the public API surface, with the
    webhook authenticated by a real HMAC-SHA512 signature — only the outbound
    Paystack initialize call is mocked (it requires live credentials).

    pricing → checkout → deposit initialize → signed webhook → confirmed →
    top-up → signed webhook → fully paid → cancel → refund computed.
    """

    def setUp(self):
        django_cache.clear()
        self.client = APIClient()

        self.package = usd_package(
            available_from=date(2027, 1, 4),
            available_to=date(2027, 1, 13),
            early_bird_deadline=timezone.now() + timedelta(days=30),
            allow_installments=True,
            deposit_minimum=Decimal("1000.00"),
            final_payment_deadline=date(2026, 11, 15),
            visa_addon_enabled=True,
            visa_fee=Decimal("150.00"),
            fx_mode="manual",
            charge_exchange_rate=Decimal("15.0000"),
            refund_tiers=[
                {"min_days": 60, "percent": 90},
                {"min_days": 30, "percent": 60},
                {"min_days": 14, "percent": 40},
                {"min_days": 0, "percent": 0},
            ],
        )
        self.option = PackageOption.objects.create(
            package=self.package,
            hotel_name="Accra Marriott", star_rating=5,
            occupancy=PackageOption.Occupancy.DOUBLE,
            price_per_person=Decimal("2500.00"),
            early_bird_price_per_person=Decimal("2200.00"),
        )
        for policy_type in ["terms", "installment", "refund", "privacy"]:
            PolicyDocument.objects.create(
                type=policy_type, version="1.0", title=policy_type, body="…",
                is_current=True, published_at=timezone.now(),
            )

    def _signed_webhook(self, reference, ghs_amount):
        """POST the webhook exactly as Paystack would: raw body signed with
        HMAC-SHA512 in X-Paystack-Signature. The handler re-confirms via
        GET /transaction/verify before applying (never trusts the payload),
        so the gateway's verify response is mocked to match."""
        payload = json_lib.dumps({
            "event": "charge.success",
            "data": {
                "reference": reference,
                "status": "success",
                "amount": to_subunits(ghs_amount),
                "currency": "GHS",
            },
        }).encode()
        signature = hmac_lib.new(b"sk_test_e2e_secret", payload, hashlib.sha512).hexdigest()
        with patch("payments.views.verify_transaction") as mock_verify:
            mock_verify.return_value = {
                "reference": reference,
                "status": "success",
                "amount": to_subunits(ghs_amount),
                "currency": "GHS",
            }
            return self.client.generic(
                "POST", "/api/payments/webhook/", payload,
                content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=signature,
            )

    @patch("payments.views.initialize_transaction")
    def test_installment_journey_end_to_end(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}

        # 1 — pricing matrix
        r = self.client.get(f"/api/packages/{self.package.id}/pricing/")
        self.assertEqual(r.status_code, 200)
        marriott_double = next(o for o in r.data["options"] if o["occupancy"] == "double")
        self.assertEqual(marriott_double["effective_total"], "4400.00")  # early bird ×2

        # 2 — checkout (visa + installments, all policies accepted)
        r = self.client.post("/api/bookings/checkout/", {
            "option_id": str(self.option.id), "visa": True, "payment_plan": "installment",
            "first_name": "Ama", "last_name": "Owusu", "email": "ama@example.com",
            "accepted_policies": ["terms", "installment", "refund", "privacy"],
            "expected_total": "4700.00",
        }, format="json")
        self.assertEqual(r.status_code, 201)
        booking_id, reference = r.data["id"], r.data["reference"]

        # 3 — deposit initialize: $1,000 ledger, GHS 15,000 to the gateway
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": booking_id, "intent": "deposit",
        }, format="json")
        self.assertEqual(r.data["amount"], "1000.00")
        self.assertEqual(r.data["charged_amount"], "15000.00")
        deposit_ref = r.data["reference"]

        # 4 — Paystack webhook (genuinely signed) → deposit confirms booking
        with self.captureOnCommitCallbacks(execute=False):
            r = self._signed_webhook(deposit_ref, Decimal("15000.00"))
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/payments/status/{deposit_ref}/")
        self.assertEqual(r.data["status"], "success")
        self.assertEqual(r.data["booking_status"], "confirmed")
        self.assertEqual(r.data["balance"], "3700.00")

        # tampered signature is rejected
        bad = self.client.generic(
            "POST", "/api/payments/webhook/", b'{"event":"charge.success"}',
            content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE="forged",
        )
        self.assertEqual(bad.status_code, 400)

        # duplicate webhook delivery is a no-op
        with self.captureOnCommitCallbacks(execute=False):
            self._signed_webhook(deposit_ref, Decimal("15000.00"))
        r = self.client.get(f"/api/bookings/{reference}/")
        self.assertEqual(r.data["amount_paid"], "1000.00")  # not double-credited

        # 5 — customer tops up the remaining balance from the dashboard
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": booking_id, "intent": "balance",
        }, format="json")
        self.assertEqual(r.data["amount"], "3700.00")
        self.assertEqual(r.data["charged_amount"], "55500.00")
        with self.captureOnCommitCallbacks(execute=False):
            self._signed_webhook(r.data["reference"], Decimal("55500.00"))

        r = self.client.get(f"/api/bookings/{reference}/")
        self.assertEqual(r.data["payment_state"], "fully_paid")
        self.assertEqual(r.data["balance"], "0.00")
        self.assertEqual(len([p for p in r.data["payments"] if p["status"] == "success"]), 2)

        # further payment attempts are refused
        r = self.client.post("/api/payments/initialize/", {"booking_id": booking_id}, format="json")
        self.assertEqual(r.status_code, 400)

        # 6 — cancellation: tiers apply to the USD ledger, visa excluded
        from bookings.models import Booking
        from bookings.services import cancel_booking

        booking = Booking.objects.get(pk=booking_id)
        at = timezone.now()  # departure 2027-01-04 → 60+ days out → 90% tier
        cancel_booking(booking, reason=Booking.CancellationReason.CUSTOMER)
        booking.refresh_from_db()

        pending = booking.refunds.filter(status=Refund.Status.PENDING)
        total_refund = sum(leg.amount for leg in pending)
        # (4700 paid − 300 non-refundable visa) × 90% = 3960.00 USD
        self.assertEqual(total_refund, Decimal("3960.00"))
        self.assertEqual(pending.first().breakdown["non_refundable_components"], "300.00")


# ── "No room for error" hardening: hard bounds, refund GHS guidance, alerts ──

class FxHardBoundTests(TestCase):
    def setUp(self):
        django_cache.clear()

    @patch("payments.fx.requests.get")
    def test_first_ever_fetch_rejected_when_outside_hard_bounds(self, mock_get):
        """A poisoned feed on a FRESH database (no history for the relative
        check) is still rejected by the absolute plausibility band."""
        self.assertEqual(FxRate.objects.count(), 0)
        mock_get.return_value = provider_ok(1.12)  # absurd for USD->GHS

        resolved = get_market_rate("USD", "GHS")
        self.assertIsNone(resolved)                # both providers' value rejected
        self.assertEqual(FxRate.objects.count(), 0)  # garbage never stored

    @patch("payments.fx.requests.get")
    def test_first_ever_fetch_accepted_inside_hard_bounds(self, mock_get):
        mock_get.return_value = provider_ok(11.25)
        resolved = get_market_rate("USD", "GHS")
        self.assertEqual(resolved.rate, Decimal("11.250000"))

    @patch("payments.fx.requests.get")
    def test_absurdly_high_rate_rejected(self, mock_get):
        mock_get.return_value = provider_ok(1125.0)  # decimal shift upward
        self.assertIsNone(get_market_rate("USD", "GHS"))


class RefundGatewayGuidanceTests(TestCase):
    def test_refund_legs_carry_exact_ghs_gateway_amounts(self):
        """Operators execute refunds in GHS on Paystack — every leg must state
        the exact proportional GHS figure, no mental arithmetic."""
        booking = make_booking(total="1000.00", status=Booking.Status.CONFIRMED)
        booking.currency = "USD"
        booking.travel_date = timezone.now().date() + timedelta(days=90)
        booking.refund_tiers_snapshot = REFUND_TIERS
        booking.save()

        # $1,000 charged as GHS 15,000 (rate 15.0)
        payment = Payment.objects.create(
            booking=booking,
            paystack_reference=generate_payment_reference(booking),
            amount=Decimal("1000.00"), currency="USD",
            charged_amount=Decimal("15000.00"), charged_currency="GHS",
            exchange_rate=Decimal("15.0000"),
        )
        with patch("payments.email.send_booking_confirmation"):
            apply_successful_payment(payment, {
                "amount": 1500000, "currency": "GHS", "status": "success",
            })
        booking.refresh_from_db()

        (leg,) = create_pending_refunds(booking, reason="customer cancelled")
        # 90% tier → $900 ledger refund → proportionally GHS 13,500 on the gateway
        self.assertEqual(leg.amount, Decimal("900.00"))
        self.assertIn("Refund GHS 13500.00", leg.breakdown["execute_on_gateway"])
        self.assertIn(payment.paystack_reference, leg.breakdown["execute_on_gateway"])


class AlertTests(TestCase):
    def setUp(self):
        django_cache.clear()

    def test_alert_never_raises_and_is_throttled(self):
        from .alerts import alert_admin

        # Log-only mode (no recipient/API key configured) — must be a no-op
        # that never raises inside a payment flow.
        alert_admin("test-key", "subject", "body")
        alert_admin("test-key", "subject", "body")

    @patch("payments.alerts.alert_admin")
    def test_gateway_mismatch_alerts_admin(self, mock_alert):
        # Patch at source module: services imported the name directly.
        with patch("payments.services.alert_admin") as mock_service_alert:
            booking = make_booking()
            payment = make_payment(booking)
            with self.captureOnCommitCallbacks(execute=True):
                apply_successful_payment(payment, {"amount": 5, "currency": "GHS", "status": "success"})
            mock_service_alert.assert_called_once()
            self.assertIn("gateway-mismatch", mock_service_alert.call_args[0][0])


# ── In-app scheduler + admin-configurable ops settings ───────────────────────

class AutoTaskTests(TestCase):
    def setUp(self):
        django_cache.clear()

    def test_due_task_claimed_once_across_concurrent_calls(self):
        """The row-lock claim means two workers checking simultaneously run a
        task once, not twice."""
        from payments import autotasks
        from payments.models import ScheduledTask

        runs = []
        with patch.dict(autotasks.TASKS, {
            "refresh_fx_rates": (lambda: timedelta(seconds=3600), lambda: runs.append(1) or "ok"),
            "send_payment_reminders": (lambda: timedelta(hours=24), lambda: "ok"),
        }), patch.object(autotasks.threading, "Thread") as mock_thread:
            # Threads run synchronously for determinism
            mock_thread.side_effect = lambda target, args, daemon: type(
                "T", (), {"start": lambda self: target(*args)}
            )()
            autotasks.run_due_tasks()
            autotasks.run_due_tasks()  # second worker: interval not elapsed → skip

        self.assertEqual(len(runs), 1)
        task = ScheduledTask.objects.get(name="refresh_fx_rates")
        self.assertIsNotNone(task.last_run_at)
        self.assertEqual(task.last_result, "ok")

    def test_disabled_task_never_runs(self):
        from payments import autotasks
        from payments.models import ScheduledTask

        ScheduledTask.objects.create(name="refresh_fx_rates", is_enabled=False)
        runs = []
        with patch.dict(autotasks.TASKS, {
            "refresh_fx_rates": (lambda: timedelta(0), lambda: runs.append(1)),
        }, clear=True):
            autotasks.run_due_tasks()
        self.assertEqual(runs, [])

    def test_failing_task_records_error_and_does_not_raise(self):
        from payments import autotasks
        from payments.models import ScheduledTask

        def boom():
            raise RuntimeError("provider exploded")

        with patch.dict(autotasks.TASKS, {
            "refresh_fx_rates": (lambda: timedelta(seconds=3600), boom),
        }, clear=True), patch.object(autotasks.threading, "Thread") as mock_thread:
            mock_thread.side_effect = lambda target, args, daemon: type(
                "T", (), {"start": lambda self: target(*args)}
            )()
            autotasks.run_due_tasks()  # must not raise

        task = ScheduledTask.objects.get(name="refresh_fx_rates")
        self.assertIn("provider exploded", task.last_result)

    def test_middleware_disabled_in_tests(self):
        from django.conf import settings
        self.assertFalse(settings.AUTOTASKS_ENABLED)


class OpsConfigTests(TestCase):
    def test_singleton(self):
        from payments.models import OpsConfig

        a = OpsConfig.get()
        a.alert_email = "ops@azura.com"
        a.save()
        b = OpsConfig.get()
        self.assertEqual(b.pk, 1)
        self.assertEqual(b.alert_email, "ops@azura.com")
        self.assertEqual(OpsConfig.objects.count(), 1)

    @override_settings(ADMIN_ALERT_EMAIL="env@azura.com", RESEND_API_KEY="rk_test")
    def test_admin_configured_email_wins_over_env(self):
        from payments.alerts import alert_admin
        from payments.models import OpsConfig

        config = OpsConfig.get()
        config.alert_email = "admin-set@azura.com"
        config.save()
        django_cache.clear()

        with patch("payments.alerts.resend") as mock_resend:
            alert_admin("k1", "s", "b")
            recipient = mock_resend.Emails.send.call_args[0][0]["to"]
        self.assertEqual(recipient, ["admin-set@azura.com"])

    @override_settings(ADMIN_ALERT_EMAIL="env@azura.com", RESEND_API_KEY="rk_test")
    def test_env_fallback_when_admin_field_empty(self):
        from payments.alerts import alert_admin

        django_cache.clear()
        with patch("payments.alerts.resend") as mock_resend:
            alert_admin("k2", "s", "b")
            recipient = mock_resend.Emails.send.call_args[0][0]["to"]
        self.assertEqual(recipient, ["env@azura.com"])


# ── Paystack channel engineering: direct MoMo, Telecel routing, webhook trust ─

class MomoChannelTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _booking(self):
        return make_booking(total="1200.00")

    @patch("payments.views.charge_mobile_money")
    def test_direct_momo_pushes_prompt_no_redirect(self, mock_charge):
        mock_charge.return_value = {
            "status": "pay_offline",
            "display_text": "Please approve the prompt on your phone.",
            "reference": "x",
        }
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "channel": "momo",
            "momo_phone": "233551234567", "momo_provider": "mtn",
        }, format="json")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["channel"], "momo")
        self.assertIn("approve the prompt", r.data["payment_prompt"])
        self.assertIsNone(r.data["authorization_url"])  # no redirect for direct MoMo

        _, kwargs = mock_charge.call_args
        self.assertEqual(kwargs["phone"], "233551234567")
        self.assertEqual(kwargs["provider"], "mtn")

    @patch("payments.views.initialize_transaction")
    def test_telecel_routed_to_hosted_momo_page(self, mock_init):
        """Telecel's direct charge needs Paystack's own OTP UI — it goes
        through hosted checkout restricted to mobile_money."""
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "channel": "momo",
            "momo_phone": "0551234567", "momo_provider": "vod",
        }, format="json")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["authorization_url"], "https://p/x")
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["channels"], ["mobile_money"])

    @patch("payments.views.initialize_transaction")
    def test_card_channel_restricts_to_card(self, mock_init):
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "channel": "card",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["channels"], ["card"])

    @patch("payments.views.initialize_transaction")
    def test_default_channel_offers_card_and_momo_in_popup(self, mock_init):
        """No channel → the Paystack popup offers card AND mobile money; the
        buyer picks the network and number inside Paystack."""
        mock_init.return_value = {"access_code": "a", "authorization_url": "https://p/x"}
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id),
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["channel"], "any")
        self.assertEqual(r.data["access_code"], "a")
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["channels"], ["card", "mobile_money"])

    def test_momo_requires_phone_and_provider(self):
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "channel": "momo",
        }, format="json")
        self.assertEqual(r.status_code, 400)

    @patch("payments.views.charge_mobile_money")
    def test_momo_immediate_decline_fails_cleanly(self, mock_charge):
        mock_charge.return_value = {"status": "failed", "gateway_response": "Insufficient funds"}
        booking = self._booking()
        r = self.client.post("/api/payments/initialize/", {
            "booking_id": str(booking.id), "channel": "momo",
            "momo_phone": "0551234567", "momo_provider": "atl",
        }, format="json")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(booking.payments.get().status, Payment.Status.FAILED)

    def test_channels_endpoint(self):
        r = self.client.get("/api/payments/channels/")
        self.assertEqual(r.status_code, 200)
        momo = next(c for c in r.data["channels"] if c["code"] == "momo")
        self.assertEqual({p["code"] for p in momo["providers"]}, {"mtn", "atl", "vod"})


class WebhookDistrustTests(TestCase):
    """The webhook signature proves the SENDER; only verify proves the MONEY."""

    def _signed(self, payload_dict, secret=b"sk_test_wh"):
        import hashlib as _hashlib
        import hmac as _hmac
        import json as _json
        payload = _json.dumps(payload_dict).encode()
        sig = _hmac.new(secret, payload, _hashlib.sha512).hexdigest()
        return payload, sig

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_wh")
    @patch("payments.views.verify_transaction")
    def test_webhook_success_not_applied_when_verify_disagrees(self, mock_verify):
        """A forged-or-mistaken 'success' webhook with a valid signature must
        still not move money if the gateway's verify says otherwise."""
        booking = make_booking()
        payment = make_payment(booking)
        mock_verify.return_value = {"status": "failed", "amount": 0, "currency": "GHS"}

        payload, sig = self._signed({
            "event": "charge.success",
            "data": {"reference": payment.paystack_reference, "status": "success",
                     "amount": to_subunits(payment.amount), "currency": "GHS"},
        })
        r = APIClient().generic("POST", "/api/payments/webhook/", payload,
                                content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200)  # acknowledged…
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)   # …but nothing moved
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_wh")
    @patch("payments.views.verify_transaction")
    def test_webhook_applies_verify_data_not_payload_data(self, mock_verify):
        """Amount checks run against the gateway's verified figures."""
        booking = make_booking(total="1200.00")
        payment = make_payment(booking)
        mock_verify.return_value = {
            "status": "success",
            "amount": to_subunits(payment.amount),
            "currency": payment.currency,
        }
        # Payload lies about the amount — irrelevant, verify data is applied.
        payload, sig = self._signed({
            "event": "charge.success",
            "data": {"reference": payment.paystack_reference, "status": "success",
                     "amount": 1, "currency": "GHS"},
        })
        with patch("payments.email.send_booking_confirmation"):
            r = APIClient().generic("POST", "/api/payments/webhook/", payload,
                                    content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.amount_paid, Decimal("1200.00"))


class MsisdnTests(TestCase):
    def test_normalize_ghana_numbers(self):
        from .paystack import normalize_msisdn
        self.assertEqual(normalize_msisdn("233506718377"), "0506718377")
        self.assertEqual(normalize_msisdn("0506718377"), "0506718377")
        self.assertEqual(normalize_msisdn("506718377"), "0506718377")
        self.assertEqual(normalize_msisdn("+233 50 671 8377"), "0506718377")


class VerifyEndpointStatusMappingTests(TestCase):
    """Regression: the verify endpoint must map every gateway status to the
    right local state — in particular an `abandoned` Paystack transaction must
    become ABANDONED, never FAILED, and a customer who was successfully charged
    must never be treated as unsuccessful afterwards."""

    def _verify(self, payment, gateway_status, **extra):
        data = {"status": gateway_status,
                "amount": to_subunits(payment.charged_amount or payment.amount),
                "currency": payment.charged_currency or payment.currency}
        data.update(extra)
        with patch("payments.views.verify_transaction", return_value=data) as mock_verify:
            response = APIClient().get(f"/api/payments/verify/{payment.paystack_reference}/")
        payment.refresh_from_db()
        payment.booking.refresh_from_db()
        return response, mock_verify

    def test_gateway_abandoned_maps_to_abandoned_not_failed(self):
        payment = make_payment(make_booking())
        response, _ = self._verify(payment, "abandoned")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.ABANDONED)
        self.assertNotEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(payment.booking.status, Booking.Status.PENDING)

    def test_gateway_failed_maps_to_failed(self):
        payment = make_payment(make_booking())
        response, _ = self._verify(payment, "failed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.FAILED)

    def test_gateway_pending_and_ongoing_leave_payment_pending(self):
        for gateway_status in ("pending", "ongoing", "pay_offline", "queued"):
            payment = make_payment(make_booking())
            response, _ = self._verify(payment, gateway_status)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payment.status, Payment.Status.PENDING,
                             f"gateway '{gateway_status}' must not change PENDING")

    def test_gateway_success_confirms_booking_and_sends_email_once(self):
        payment = make_payment(make_booking())
        with patch("payments.email.send_booking_confirmation") as mock_email:
            with self.captureOnCommitCallbacks(execute=True):
                response, _ = self._verify(payment, "success")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payment.status, Payment.Status.SUCCESS)
            self.assertEqual(payment.booking.status, Booking.Status.CONFIRMED)
            self.assertEqual(mock_email.call_count, 1)

    def test_verified_success_is_terminal_no_second_gateway_call(self):
        """Once local status is SUCCESS the endpoint answers from the DB —
        a later gateway blip can never downgrade a charged customer."""
        payment = make_payment(make_booking())
        with patch("payments.email.send_booking_confirmation"):
            self._verify(payment, "success")
        response, mock_verify = self._verify(payment, "abandoned")
        self.assertEqual(response.status_code, 200)
        mock_verify.assert_not_called()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(response.data["status"], "success")

    def test_reverify_after_abandoned_can_still_succeed(self):
        """A shopper who abandons checkout, then pays on a retry of the same
        attempt, must be creditable: abandoned -> success is a legal upgrade."""
        payment = make_payment(make_booking())
        self._verify(payment, "abandoned")
        self.assertEqual(payment.status, Payment.Status.ABANDONED)
        with patch("payments.email.send_booking_confirmation"):
            self._verify(payment, "success")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(payment.booking.status, Booking.Status.CONFIRMED)


class InitializeIntentGuardTests(TestCase):
    """Regression for the retry-page bug: intent=deposit on a booking whose
    plan has no deposit must 400 cleanly and create no payment attempt."""

    @patch("payments.views.initialize_transaction")
    def test_deposit_intent_on_full_plan_booking_is_rejected(self, mock_init):
        booking = make_booking()  # payment_plan defaults to "full", no deposit_required
        response = APIClient().post("/api/payments/initialize/",
                                    {"booking_id": str(booking.id), "intent": "deposit"},
                                    format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("deposit", response.data["detail"].lower())
        mock_init.assert_not_called()
        self.assertEqual(booking.payments.count(), 0)


@override_settings(PAYSTACK_SECRET_KEY="sk_test_sec_secret")
class SecurityRegressionTests(TestCase):
    """Attacker-perspective regressions for the payment/booking surface.

    Every test here encodes an invariant a malicious client must never be able
    to break: forged webhooks, amount/status manipulation, unpaid credit,
    cross-field injection, and unauthenticated access to protected data.
    """

    def setUp(self):
        django_cache.clear()
        self.client = APIClient()

    def _sign(self, body: bytes) -> str:
        return hmac_lib.new(b"sk_test_sec_secret", body, hashlib.sha512).hexdigest()

    # ── Webhook forgery ──────────────────────────────────────────────────────
    def test_webhook_without_signature_is_rejected(self):
        booking = make_booking()
        payment = make_payment(booking)
        body = json_lib.dumps({"event": "charge.success",
                               "data": {"reference": payment.paystack_reference,
                                        "status": "success", "amount": 120000, "currency": "GHS"}}).encode()
        r = self.client.generic("POST", "/api/payments/webhook/", body, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_webhook_with_bad_signature_is_rejected(self):
        booking = make_booking()
        payment = make_payment(booking)
        body = json_lib.dumps({"event": "charge.success",
                               "data": {"reference": payment.paystack_reference,
                                        "status": "success", "amount": 120000, "currency": "GHS"}}).encode()
        r = self.client.generic("POST", "/api/payments/webhook/", body,
                                content_type="application/json",
                                HTTP_X_PAYSTACK_SIGNATURE="deadbeef" * 16)
        self.assertEqual(r.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_forged_success_webhook_not_applied_when_gateway_disagrees(self):
        """Valid signature + a 'success' payload, but the gateway's own verify
        says the charge never succeeded → money must NOT be credited."""
        booking = make_booking()
        payment = make_payment(booking)
        body = json_lib.dumps({"event": "charge.success",
                               "data": {"reference": payment.paystack_reference,
                                        "status": "success", "amount": 120000, "currency": "GHS"}}).encode()
        with patch("payments.views.verify_transaction",
                   return_value={"reference": payment.paystack_reference,
                                 "status": "abandoned", "amount": 120000, "currency": "GHS"}):
            r = self.client.generic("POST", "/api/payments/webhook/", body,
                                    content_type="application/json",
                                    HTTP_X_PAYSTACK_SIGNATURE=self._sign(body))
        self.assertEqual(r.status_code, 200)
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertNotEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_duplicate_success_webhook_credits_once(self):
        booking = make_booking(total="1200.00")
        payment = make_payment(booking)
        body = json_lib.dumps({"event": "charge.success",
                               "data": {"reference": payment.paystack_reference,
                                        "status": "success", "amount": 120000, "currency": "GHS"}}).encode()
        gw = {"reference": payment.paystack_reference, "status": "success",
              "amount": to_subunits(payment.amount), "currency": payment.currency}
        with patch("payments.views.verify_transaction", return_value=gw), \
                patch("payments.email.send_booking_confirmation") as mock_email:
            with self.captureOnCommitCallbacks(execute=True):
                for _ in range(3):
                    r = self.client.generic("POST", "/api/payments/webhook/", body,
                                            content_type="application/json",
                                            HTTP_X_PAYSTACK_SIGNATURE=self._sign(body))
                    self.assertEqual(r.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("1200.00"))          # credited once
        self.assertEqual(booking.payments.filter(status=Payment.Status.SUCCESS).count(), 1)
        self.assertEqual(mock_email.call_count, 1)                          # one confirmation email

    # ── Amount / field manipulation on initialize ────────────────────────────
    def test_initialize_ignores_client_supplied_amount_and_currency(self):
        booking = make_booking(total="1200.00")
        with patch("payments.views.initialize_transaction",
                   return_value={"access_code": "a", "authorization_url": "https://p/x"}):
            r = self.client.post("/api/payments/initialize/", {
                "booking_id": str(booking.id), "intent": "balance",
                "amount": "1.00", "currency": "NGN",
                "charged_amount": "1.00", "status": "success", "needs_review": False,
            }, format="json")
        self.assertEqual(r.status_code, 200)
        payment = booking.payments.latest("created_at")
        self.assertEqual(payment.amount, Decimal("1200.00"))               # server-computed, not 1.00
        self.assertEqual(payment.currency, booking.currency)              # not NGN
        self.assertEqual(payment.status, Payment.Status.PENDING)          # not forced success
        self.assertFalse(payment.needs_review)

    def test_checkout_rejects_understated_expected_total(self):
        """A client that tampers expected_total to pay less gets a 409 with the
        server's authoritative quote — never a cheap booking."""
        package = usd_package(early_bird_deadline=timezone.now() + timedelta(days=30))
        from packages.models import PackageOption
        option = PackageOption.objects.create(
            package=package, hotel_name="H", star_rating=4,
            occupancy=PackageOption.Occupancy.SINGLE,
            price_per_person=Decimal("2000.00"),
            early_bird_price_per_person=Decimal("1800.00"),
        )
        for pt in ["terms", "installment", "refund", "privacy"]:
            PolicyDocument.objects.create(type=pt, version="1.0", title=pt, body="x",
                                          is_current=True, published_at=timezone.now())
        r = self.client.post("/api/bookings/checkout/", {
            "option_id": str(option.id), "visa": False, "payment_plan": "full",
            "first_name": "M", "last_name": "O", "email": "m@example.com",
            "accepted_policies": ["terms", "installment", "refund", "privacy"],
            "expected_total": "1.00",
        }, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Booking.objects.filter(email="m@example.com").count(), 0)

    # ── Unpaid / failed / abandoned can't be turned into success by a client ──
    def test_verify_does_not_credit_unpaid_transaction(self):
        booking = make_booking()
        payment = make_payment(booking)
        with patch("payments.views.verify_transaction",
                   return_value={"status": "abandoned",
                                 "amount": to_subunits(payment.amount), "currency": payment.currency}):
            r = self.client.get(f"/api/payments/verify/{payment.paystack_reference}/")
        self.assertEqual(r.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.amount_paid, Decimal("0.00"))

    def test_verify_query_params_cannot_force_success(self):
        booking = make_booking()
        payment = make_payment(booking)
        with patch("payments.views.verify_transaction",
                   return_value={"status": "failed",
                                 "amount": to_subunits(payment.amount), "currency": payment.currency}):
            r = self.client.get(
                f"/api/payments/verify/{payment.paystack_reference}/?status=success&amount=1")
        self.assertEqual(r.data["status"], "failed")

    # ── Excessive data exposure / unauthenticated access ─────────────────────
    def test_status_endpoint_reachable_without_auth_but_no_pii(self):
        """PaymentStatus is a public poll surface by design; assert it never
        leaks customer name/email/phone even though it is unauthenticated."""
        booking = make_booking()
        payment = make_payment(booking)
        r = self.client.get(f"/api/payments/status/{payment.paystack_reference}/")
        self.assertEqual(r.status_code, 200)
        for leaky in ("email", "first_name", "last_name", "phone"):
            self.assertNotIn(leaky, r.data)

    def test_mine_requires_authentication(self):
        r = self.client.get("/api/bookings/mine/")
        self.assertEqual(r.status_code, 401)

    def test_registration_cannot_self_grant_staff(self):
        r = self.client.post("/api/auth/register/", {
            "email": "escalate@test.com", "password": "Str0ng#Pass1",
            "password_confirm": "Str0ng#Pass1", "first_name": "E", "last_name": "S",
            "is_staff": True, "is_superuser": True, "is_active": True,
        }, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("password", r.data)
        user = User.objects.get(email="escalate@test.com")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)


class ConfirmationEmailEscapingTests(TestCase):
    """Regression for HTML injection in the confirmation email: user-controlled
    fields must be HTML-escaped, never interpolated raw into the message body."""

    def test_user_fields_are_html_escaped_in_confirmation_email(self):
        from payments.email import _build_html
        booking = make_booking()
        booking.refresh_from_db()  # travel_date as a real date object, not the fixture string
        booking.first_name = "<script>alert(1)</script>"
        booking.last_name = '"><img src=x onerror=alert(2)>'
        booking.special_requests = '<a href="https://evil.example/phish">Click</a>'
        payment = make_payment(booking, status=Payment.Status.SUCCESS)
        html_out = _build_html(booking, payment)
        # No live tags survive …
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertNotIn("<img src=x onerror", html_out)
        self.assertNotIn('<a href="https://evil.example/phish"', html_out)
        # … they are present only as escaped entities.
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("&lt;img src=x onerror", html_out)


class PaymentBreakdownTests(TestCase):
    """The accounting breakdown must decompose a booking into its priced
    components and always reconcile to the stored total."""

    def _visa_booking(self):
        booking = make_booking(total="4100.00")
        booking.unit_price = Decimal("1900.00")
        booking.num_guests = 2
        booking.early_bird_discount = Decimal("400.00")
        booking.option_snapshot = {"hotel_name": "Accra Marriott", "occupancy": "double",
                                   "occupancy_display": "Couple / Double Occupancy"}
        booking.addons = [{"code": "visa", "name": "Visa on Arrival", "unit_price": "150.00",
                           "quantity": 2, "line_total": "300.00", "refundable": False}]
        booking.save()
        return booking

    def test_breakdown_reconciles_package_plus_addons(self):
        from payments.receipts import compute_line_items
        bd = compute_line_items(self._visa_booking())
        labels = {l["category"]: l["amount"] for l in bd["lines"]}
        self.assertEqual(labels["package"], Decimal("3800.00"))   # 1900 × 2
        self.assertEqual(labels["visa"], Decimal("300.00"))
        self.assertEqual(bd["component_sum"], Decimal("4100.00"))
        self.assertEqual(bd["total"], Decimal("4100.00"))
        self.assertTrue(bd["reconciles"])
        self.assertEqual(bd["discount"], Decimal("400.00"))

    def test_breakdown_flags_non_reconciling_snapshot(self):
        from payments.receipts import compute_line_items
        booking = self._visa_booking()
        booking.total_amount = Decimal("9999.00")  # drift
        booking.save()
        bd = compute_line_items(booking)
        self.assertFalse(bd["reconciles"])

    def test_package_only_booking_single_line(self):
        from payments.receipts import compute_line_items
        booking = make_booking(total="1200.00")
        booking.unit_price = Decimal("600.00"); booking.num_guests = 2; booking.save()
        bd = compute_line_items(booking)
        self.assertEqual(len(bd["lines"]), 1)
        self.assertEqual(bd["lines"][0]["category"], "package")
        self.assertTrue(bd["reconciles"])


class ReceiptGenerationTests(TestCase):
    def _paid_booking(self):
        booking = make_booking(total="1200.00", status=Booking.Status.CONFIRMED)
        booking.first_name = "Ama"; booking.last_name = "Owusu"; booking.amount_paid = Decimal("1200.00")
        booking.save()
        payment = make_payment(booking, status=Payment.Status.SUCCESS)
        payment.paid_at = timezone.now(); payment.save()
        return booking, payment

    def test_receipt_matches_payment_record(self):
        from payments.receipts import render_receipt_html, receipt_number
        booking, payment = self._paid_booking()
        html = render_receipt_html(payment)
        self.assertIn(booking.reference, html)
        self.assertIn(payment.paystack_reference, html)
        self.assertIn("Ama Owusu", html)
        self.assertIn("1,200.00", html)
        self.assertIn(booking.currency, html)
        self.assertIn(receipt_number(payment)[:8], html)

    def test_receipt_escapes_user_fields(self):
        from payments.receipts import render_receipt_html
        booking, payment = self._paid_booking()
        booking.first_name = "<script>alert(1)</script>"; booking.save()
        html = render_receipt_html(payment)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_success_receipt_downloadable_by_staff(self):
        from django.test import Client
        from accounts.models import User
        booking, payment = self._paid_booking()
        staff = User.objects.create_user(email="staff_rcpt@test.com", password="x", is_staff=True, is_superuser=True)
        c = Client(); c.force_login(staff)
        r = c.get(f"/admin/payments/payment/{payment.id}/receipt/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"What this paid for", r.content)

    def test_no_paid_receipt_for_unsuccessful_payment(self):
        from django.test import Client
        from accounts.models import User
        booking = make_booking(total="1200.00")
        payment = make_payment(booking, status=Payment.Status.ABANDONED)
        staff = User.objects.create_user(email="staff_rcpt2@test.com", password="x", is_staff=True, is_superuser=True)
        c = Client(); c.force_login(staff)
        r = c.get(f"/admin/payments/payment/{payment.id}/receipt/")
        self.assertEqual(r.status_code, 409)

    def test_receipt_requires_staff(self):
        from django.test import Client
        from accounts.models import User
        booking, payment = self._paid_booking()
        r_anon = Client().get(f"/admin/payments/payment/{payment.id}/receipt/")
        self.assertIn(r_anon.status_code, (302, 403))          # redirected to admin login
        user = User.objects.create_user(email="plain_rcpt@test.com", password="x", is_staff=False)
        c = Client(); c.force_login(user)
        r_user = c.get(f"/admin/payments/payment/{payment.id}/receipt/")
        self.assertIn(r_user.status_code, (302, 403))          # non-staff cannot reach admin


@override_settings(PAYSTACK_SECRET_KEY="sk_test_tmpl_secret")
class EmailTemplateSelectionTests(TestCase):
    """The right email fires for the right action: a deposit that promotes the
    booking → confirmation; a later top-up on an already-confirmed booking →
    receipt. Never both, never the wrong one."""

    def test_promoting_payment_sends_confirmation_not_receipt(self):
        booking = make_booking(total="1200.00")
        payment = make_payment(booking)
        with patch("payments.email.send_booking_confirmation") as conf, \
                patch("payments.email.send_payment_receipt") as rcpt:
            with self.captureOnCommitCallbacks(execute=True):
                apply_successful_payment(payment, gateway_ok(payment))
        self.assertEqual(conf.call_count, 1)
        self.assertEqual(rcpt.call_count, 0)

    def test_topup_on_confirmed_booking_sends_receipt_not_confirmation(self):
        booking = make_booking(total="1200.00")
        first = make_payment(booking, amount=Decimal("600.00"))
        with patch("payments.email.send_booking_confirmation"), patch("payments.email.send_payment_receipt"), \
                self.captureOnCommitCallbacks(execute=True):
            apply_successful_payment(first, gateway_ok(first))   # 600 of 1200 — not confirmed yet
        # Force confirmation via a deposit threshold is not set, so 600 stays pending;
        # pay the rest → this promotes. Instead test an already-confirmed booking:
        booking.refresh_from_db()
        booking.status = Booking.Status.CONFIRMED; booking.save()
        second = make_payment(booking, amount=Decimal("600.00"))
        with patch("payments.email.send_booking_confirmation") as conf, \
                patch("payments.email.send_payment_receipt") as rcpt:
            with self.captureOnCommitCallbacks(execute=True):
                apply_successful_payment(second, gateway_ok(second))
        self.assertEqual(conf.call_count, 0)
        self.assertEqual(rcpt.call_count, 1)


# ── Receipt QR code ──────────────────────────────────────────────────────────

from django.test import override_settings


@override_settings(FRONTEND_URL="https://front.example/")
class BookingQrCodeTests(TestCase):
    def test_qr_points_at_frontend_not_api_host(self):
        """The QR PNG is served by the API, but scanning it must open the
        frontend's verification page — never the backend host."""
        from .receipts import booking_verify_url

        booking = make_booking(total="1200.00")
        self.assertEqual(
            booking_verify_url(booking),
            f"https://front.example/payment/callback?reference={booking.reference}",
        )

        r = APIClient().get(f"/api/payments/bookings/{booking.reference}/qr.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")

    def test_qr_unknown_reference_404(self):
        r = APIClient().get("/api/payments/bookings/AZT-NOPE/qr.png")
        self.assertEqual(r.status_code, 404)


# ── Installment / Refund Policy: overdue notice, cancellation requests, line items ─

from django.test import override_settings


@override_settings(RESEND_API_KEY="test-key", RESEND_FROM_EMAIL="t@test")
class OverdueNoticeTests(TestCase):
    def _overdue_booking(self, days_past=1):
        booking = make_booking(total="1200.00", status=Booking.Status.CONFIRMED)
        booking.package.final_payment_deadline = date.today() - timedelta(days=days_past)
        booking.package.save()
        return booking

    @patch("payments.email.resend")
    def test_one_notice_then_idempotent(self, mock_resend):
        from django.core.management import call_command
        booking = self._overdue_booking()
        call_command("send_overdue_notices")
        call_command("send_overdue_notices")           # second run must be a no-op
        self.assertEqual(mock_resend.Emails.send.call_count, 1)
        booking.refresh_from_db()
        self.assertIsNotNone(booking.overdue_notice_sent_at)
        self.assertEqual(booking.cure_deadline, booking.overdue_notice_sent_at + timedelta(hours=72))
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)   # never auto-cancelled

    @patch("payments.email.resend")
    def test_not_overdue_gets_nothing(self, mock_resend):
        from django.core.management import call_command
        booking = make_booking(status=Booking.Status.CONFIRMED)
        booking.package.final_payment_deadline = date.today() + timedelta(days=5)
        booking.package.save()
        call_command("send_overdue_notices")
        self.assertEqual(mock_resend.Emails.send.call_count, 0)

    def test_reminder_marks_follow_the_policy(self):
        from bookings.management.commands.send_payment_reminders import REMINDER_MARKS
        self.assertEqual(REMINDER_MARKS, [14, 7, 1])


@patch("payments.email._send")
class CancellationRequestTests(TestCase):
    def _owned(self, **kw):
        user = User.objects.create_user(email=f"owner{User.objects.count()}@test.com", password="x",
                                        first_name="O", last_name="W")
        booking = make_refundable_booking(["1000.00"], **kw)
        booking.user = user
        booking.save(update_fields=["user"])
        client = APIClient(); client.force_authenticate(user)
        return user, booking, client

    def test_quote_matches_policy_band(self, _mail):
        _, booking, client = self._owned(days_to_departure=45)
        r = client.get(f"/api/bookings/{booking.reference}/cancellation-quote/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["eligible"])
        self.assertEqual(r.data["refund_total"], "600.00")          # 60% of $1,000 paid

    def test_other_users_booking_is_invisible(self, _mail):
        _, booking, _ = self._owned()
        stranger = User.objects.create_user(email="s@test.com", password="x", first_name="S", last_name="T")
        c = APIClient(); c.force_authenticate(stranger)
        self.assertEqual(c.get(f"/api/bookings/{booking.reference}/cancellation-quote/").status_code, 404)
        self.assertEqual(c.post(f"/api/bookings/{booking.reference}/cancellation-request/", {}).status_code, 404)

    def test_request_freezes_payments_and_refuses_duplicates(self, mail):
        _, booking, client = self._owned(days_to_departure=45)
        r = client.post(f"/api/bookings/{booking.reference}/cancellation-request/",
                        {"reason": "Change of plans"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["refund_total"], "600.00")
        self.assertEqual(mail.call_count, 1)                          # dated acknowledgment
        # duplicate → refused
        r2 = client.post(f"/api/bookings/{booking.reference}/cancellation-request/", {}, format="json")
        self.assertEqual(r2.status_code, 400)
        # payments paused while under review
        p = APIClient().post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        self.assertEqual(p.status_code, 400)
        self.assertIn("under review", p.data["detail"])
        # API surfaces it
        d = APIClient().get(f"/api/bookings/{booking.reference}/")
        self.assertEqual(d.data["cancellation_request"]["status"], "pending")

    def test_approval_refunds_as_of_request_time_not_approval_time(self, mail):
        from bookings.services import approve_cancellation, request_cancellation
        staff = User.objects.create_user(email="staff@test.com", password="x", first_name="S", last_name="F")
        _, booking, _ = self._owned(days_to_departure=28)           # today → 40% band
        req = request_cancellation(booking, reason="x")
        # Pretend the request arrived 3 days ago (31 days out → 60% band)
        req.requested_at = timezone.now() - timedelta(days=3)
        req.save(update_fields=["requested_at"])
        approve_cancellation(req, by_user=staff, note="Sorry to see you go")
        booking.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.cancellation_reason, Booking.CancellationReason.CUSTOMER)
        legs_total = sum(l.amount for l in booking.refunds.all())
        self.assertEqual(legs_total, Decimal("600.00"))              # not the 400 of today's band
        self.assertEqual(req.status, "approved")
        self.assertEqual(mail.call_count, 2)                          # ack + outcome

    def test_rejection_keeps_booking_and_reopens_payments(self, _mail):
        from bookings.services import reject_cancellation, request_cancellation
        staff = User.objects.create_user(email="staff2@test.com", password="x", first_name="S", last_name="F")
        _, booking, _ = self._owned()
        req = request_cancellation(booking)
        reject_cancellation(req, by_user=staff, note="Outside policy")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIsNone(booking.pending_cancellation)


class LineItemsApiTests(TestCase):
    def test_breakdown_reconciles_to_total(self):
        booking = make_refundable_booking(
            ["500.00"], total="4700.00",
            addons=[{"code": "visa", "name": "Visa on Arrival", "unit_price": "100.00",
                     "quantity": 2, "line_total": "200.00", "refundable": False}],
        )
        booking.unit_price = Decimal("2250.00"); booking.save()   # 2 × 2250 + 200 = 4700
        r = APIClient().get(f"/api/bookings/{booking.reference}/")
        self.assertEqual(r.status_code, 200)
        li = r.data["line_items"]
        lines_sum = sum(Decimal(l["amount"]) for l in li["lines"])
        self.assertEqual(lines_sum - Decimal(li["discount"]), Decimal(li["total"]))
        self.assertEqual(li["total"], r.data["total_amount"])
        self.assertEqual(len(li["lines"]), 2)


# ── Staff permissions + the refund rules they gate ───────────────────────────

class ArrivalAndOrganizerRefundTests(TestCase):
    def test_arrived_guest_gets_nothing_on_voluntary_cancellation(self):
        # Appendix D: "Early arrival then cancellation → voluntary refund is zero"
        booking = make_refundable_booking(["1000.00"], days_to_departure=45)   # would be 60%
        booking.arrived_at = timezone.now() - timedelta(days=1)
        booking.save(update_fields=["arrived_at"])
        computed = compute_refund(booking)
        self.assertEqual(computed["refund_total"], Decimal("0.00"))
        self.assertEqual(computed["breakdown"]["tier_applied"]["basis"], "arrived_or_started")

    def test_arrival_after_request_does_not_apply(self):
        booking = make_refundable_booking(["1000.00"], days_to_departure=45)
        booking.arrived_at = timezone.now() + timedelta(days=2)               # arrives later
        booking.save(update_fields=["arrived_at"])
        self.assertEqual(compute_refund(booking)["refund_total"], Decimal("600.00"))

    def test_organizer_cancellation_refunds_in_full_regardless_of_date(self):
        from bookings.services import cancel_booking
        booking = make_refundable_booking(["1000.00"], days_to_departure=5)    # guest band → 0%
        cancel_booking(booking, reason=Booking.CancellationReason.ORGANIZER)
        legs_total = sum(l.amount for l in booking.refunds.all())
        self.assertEqual(legs_total, Decimal("1000.00"))
        self.assertEqual(booking.refunds.first().breakdown["tier_applied"]["basis"], "organizer_cancellation")

    def test_guest_cancellation_still_uses_bands(self):
        from bookings.services import cancel_booking
        booking = make_refundable_booking(["1000.00"], days_to_departure=5)
        cancel_booking(booking, reason=Booking.CancellationReason.CUSTOMER)
        self.assertEqual(booking.refunds.count(), 0)                          # 0% → no legs


class StaffGroupTests(TestCase):
    def _user_in(self, group_name):
        from django.contrib.auth.models import Group
        u = User.objects.create_user(email=f"{group_name.lower().replace(' ', '')}@test.com", password="x",
                                     first_name="S", last_name="T", is_staff=True)
        u.groups.add(Group.objects.get(name=group_name))
        return User.objects.get(pk=u.pk)  # fresh perm cache

    def setUp(self):
        from django.core.management import call_command
        call_command("setup_staff_groups")
        call_command("setup_staff_groups")   # idempotent

    def test_groups_nest_viewer_operations_finance(self):
        from django.contrib.auth.models import Group
        perms = {g.name: set(g.permissions.values_list("codename", flat=True)) for g in Group.objects.all()}
        self.assertTrue(perms["Azura Viewer"] < perms["Azura Operations"] < perms["Azura Finance"])

    def test_viewer_reads_but_cannot_act(self):
        u = self._user_in("Azura Viewer")
        self.assertTrue(u.has_perm("bookings.view_booking"))
        for p in ("payments.process_refund", "bookings.cancel_booking", "bookings.mark_arrived",
                  "bookings.resolve_cancellation", "bookings.view_special_requests", "payments.add_payment"):
            self.assertFalse(u.has_perm(p), p)

    def test_operations_runs_the_tour_but_touches_no_money(self):
        u = self._user_in("Azura Operations")
        for p in ("bookings.mark_arrived", "bookings.extend_deadline", "bookings.resolve_cancellation",
                  "bookings.view_special_requests", "payments.add_payment"):
            self.assertTrue(u.has_perm(p), p)
        for p in ("payments.process_refund", "bookings.cancel_booking", "bookings.cancel_as_organizer"):
            self.assertFalse(u.has_perm(p), p)

    def test_finance_can_do_everything_staff_can(self):
        u = self._user_in("Azura Finance")
        for p in ("payments.process_refund", "bookings.cancel_booking", "bookings.cancel_as_organizer",
                  "bookings.mark_arrived"):
            self.assertTrue(u.has_perm(p), p)
        # …but is still not a developer: no package/policy/user management
        for p in ("packages.change_travelpackage", "bookings.change_policydocument", "accounts.change_user"):
            self.assertFalse(u.has_perm(p), p)

    def test_special_requests_hidden_from_viewer_in_admin(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from bookings.admin import BookingAdmin
        viewer, ops = self._user_in("Azura Viewer"), self._user_in("Azura Operations")
        ma = BookingAdmin(Booking, AdminSite())
        def fields_for(user):
            req = RequestFactory().get("/"); req.user = user
            return {f for _, o in ma.get_fieldsets(req, None) for f in o["fields"]}
        self.assertNotIn("special_requests", fields_for(viewer))
        self.assertIn("special_requests", fields_for(ops))


# ── Staff portal API (slice 1: bookings) ─────────────────────────────────────

@patch("payments.email.send_booking_confirmation")
class StaffBookingApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        from django.core.management import call_command
        call_command("setup_staff_groups")
        def staff(group, email):
            u = User.objects.create_user(email=email, password="x", first_name="S", last_name="T", is_staff=True)
            u.groups.add(Group.objects.get(name=group))
            c = APIClient(); c.force_authenticate(User.objects.get(pk=u.pk)); return c
        self.viewer = staff("Azura Viewer", "v@test.com")
        self.ops = staff("Azura Operations", "o@test.com")
        self.guest = APIClient(); self.guest.force_authenticate(
            User.objects.create_user(email="g@test.com", password="x", first_name="G", last_name="U"))
        self.booking = make_booking(total="1200.00", status=Booking.Status.CONFIRMED)
        self.booking.special_requests = "Vegetarian; uses a wheelchair"
        self.booking.save()

    def test_non_staff_is_locked_out(self, _m):
        self.assertEqual(self.guest.get("/api/staff/bookings/").status_code, 403)
        self.assertEqual(self.guest.get("/api/staff/me/").status_code, 403)

    def test_me_lists_capabilities_by_level(self, _m):
        v = self.viewer.get("/api/staff/me/").data
        o = self.ops.get("/api/staff/me/").data
        self.assertNotIn("bookings.mark_arrived", v["perms"])
        self.assertIn("bookings.mark_arrived", o["perms"])
        self.assertEqual(v["groups"], ["Azura Viewer"])

    def test_list_searches_and_paginates(self, _m):
        r = self.viewer.get("/api/staff/bookings/", {"q": self.booking.reference[-6:]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["reference"], self.booking.reference)
        self.assertEqual(r.data["results"][0]["payment_state"], "unpaid")
        self.assertEqual(self.viewer.get("/api/staff/bookings/", {"q": "nobody-here"}).data["count"], 0)

    def test_overdue_filter(self, _m):
        self.booking.package.final_payment_deadline = date.today() - timedelta(days=1)
        self.booking.package.save()
        r = self.viewer.get("/api/staff/bookings/", {"overdue": "1"})
        self.assertEqual([b["reference"] for b in r.data["results"]], [self.booking.reference])
        self.assertTrue(r.data["results"][0]["is_overdue"])

    def test_special_requests_only_with_permission(self, _m):
        url = f"/api/staff/bookings/{self.booking.reference}/"
        self.assertNotIn("special_requests", self.viewer.get(url).data)
        self.assertEqual(self.ops.get(url).data["special_requests"], "Vegetarian; uses a wheelchair")

    def test_offline_payment_goes_through_the_ledger(self, _m):
        url = f"/api/staff/bookings/{self.booking.reference}/offline-payment/"
        self.assertEqual(self.viewer.post(url, {"amount": "200"}, format="json").status_code, 403)
        r = self.ops.post(url, {"amount": "200.00", "method": "bank_transfer", "note": "GCB slip 1234"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["amount_paid"], "200.00")
        self.assertEqual(r.data["balance"], "1000.00")
        p = self.booking.payments.get()
        self.assertEqual(p.method, "bank_transfer"); self.assertIn("recorded by o@test.com", p.note)
        # can't overpay
        bad = self.ops.post(url, {"amount": "5000"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_extend_deadline_requires_reason(self, _m):
        url = f"/api/staff/bookings/{self.booking.reference}/extend-deadline/"
        new = (date.today() + timedelta(days=10)).isoformat()
        self.assertEqual(self.ops.post(url, {"date": new}, format="json").status_code, 400)
        r = self.ops.post(url, {"date": new, "reason": "Salary delayed, agreed by phone"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["payment_deadline"], date.fromisoformat(new))
        self.assertIn("o@test.com", r.data["payment_deadline_override_reason"])
        self.assertEqual(self.viewer.post(url, {"date": new, "reason": "x"}, format="json").status_code, 403)

    def test_mark_arrived_is_idempotent_and_gated(self, _m):
        url = f"/api/staff/bookings/{self.booking.reference}/mark-arrived/"
        self.assertEqual(self.viewer.post(url).status_code, 403)
        first = self.ops.post(url).data["arrived_at"]
        second = self.ops.post(url).data["arrived_at"]
        self.assertIsNotNone(first); self.assertEqual(first, second)


# ── Staff portal API (slices 2+3: cancellations, refunds) ───────────────────

@patch("payments.email._send")
class StaffCancellationAndRefundApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        from django.core.management import call_command
        call_command("setup_staff_groups")
        def staff(group, email):
            u = User.objects.create_user(email=email, password="x", first_name="S", last_name="T", is_staff=True)
            u.groups.add(Group.objects.get(name=group))
            c = APIClient(); c.force_authenticate(User.objects.get(pk=u.pk)); return c
        self.viewer = staff("Azura Viewer", "v2@test.com")
        self.ops = staff("Azura Operations", "o2@test.com")
        self.fin = staff("Azura Finance", "f2@test.com")

    def _request(self, days=45):
        from bookings.services import request_cancellation
        booking = make_refundable_booking(["1000.00"], days_to_departure=days)
        return booking, request_cancellation(booking, reason="Can't travel")

    def test_queue_lists_pending_with_quote(self, _m):
        booking, req = self._request()
        r = self.viewer.get("/api/staff/cancellations/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["id"] for x in r.data], [str(req.id)])
        self.assertEqual(r.data[0]["refund_total"], "600.00")
        self.assertEqual(r.data[0]["booking"]["reference"], booking.reference)

    def test_viewer_cannot_resolve_operations_can(self, mail):
        booking, req = self._request()
        self.assertEqual(self.viewer.post(f"/api/staff/cancellations/{req.id}/approve/", {}, format="json").status_code, 403)
        r = self.ops.post(f"/api/staff/cancellations/{req.id}/approve/", {"note": "Approved as per policy"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "approved")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.refunds.count(), 1)                       # leg now waiting for Finance
        # already resolved → 400, queue empty
        self.assertEqual(self.ops.post(f"/api/staff/cancellations/{req.id}/reject/", {}, format="json").status_code, 400)
        self.assertEqual(self.viewer.get("/api/staff/cancellations/").data, [])

    def test_reject_keeps_booking(self, _m):
        booking, req = self._request()
        r = self.ops.post(f"/api/staff/cancellations/{req.id}/reject/", {"note": "Outside policy"}, format="json")
        self.assertEqual(r.data["status"], "rejected")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    def test_refund_queue_and_processing(self, mail):
        booking, req = self._request()
        self.ops.post(f"/api/staff/cancellations/{req.id}/approve/", {}, format="json")
        leg_id = self.fin.get("/api/staff/refunds/").data[0]["id"]
        self.assertEqual(self.fin.get("/api/staff/refunds/").data[0]["amount"], "600.00")
        # Operations can see but not pay out
        self.assertEqual(self.ops.get("/api/staff/refunds/").status_code, 200)
        self.assertEqual(self.ops.post(f"/api/staff/refunds/{leg_id}/process/", {"external_reference": "x"}, format="json").status_code, 403)
        # Finance must give a reference
        self.assertEqual(self.fin.post(f"/api/staff/refunds/{leg_id}/process/", {}, format="json").status_code, 400)
        mail.reset_mock()
        # the guest email is sent on commit — run commit hooks inside the test transaction
        with self.captureOnCommitCallbacks(execute=True):
            r = self.fin.post(f"/api/staff/refunds/{leg_id}/process/", {"external_reference": "PSK-RF-123", "note": "Done in dashboard"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "processed")
        self.assertEqual(r.data["external_reference"], "PSK-RF-123")
        booking.refresh_from_db()
        self.assertEqual(booking.amount_refunded, Decimal("600.00"))
        # guest told: amount + reference
        self.assertEqual(mail.call_count, 1)
        self.assertIn("PSK-RF-123", mail.call_args[0][2])
        self.assertEqual(self.fin.get("/api/staff/refunds/").data, [])       # nothing pending
        self.assertEqual(len(self.fin.get("/api/staff/refunds/", {"status": "processed"}).data), 1)


# ── Policy gaps: expiry, capacity, disputes, delivery log, failed email, staff cancel, misc ─

import json as _json


@override_settings(PENDING_BOOKING_TTL_HOURS=24)
class ExpiryAndCapacityTests(TestCase):
    def test_any_unpaid_pending_booking_expires(self):
        booking = make_booking()                        # legacy/flat — no hotel option
        self.assertFalse(booking.is_expired)
        Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(hours=25))
        booking.refresh_from_db()
        self.assertTrue(booking.is_expired)

    def test_capacity_counts_holders_and_refuses_overflow(self):
        from bookings.pricing import QuoteError, assert_capacity
        booking = make_booking(status=Booking.Status.CONFIRMED)   # 2 guests
        pkg = booking.package
        self.assertIsNone(pkg.spots_left)                          # unlimited by default
        pkg.capacity = 3; pkg.save()
        self.assertEqual(pkg.spots_left, 1)
        assert_capacity(pkg, 1)
        with self.assertRaises(QuoteError):
            assert_capacity(pkg, 2)
        # a fresh unpaid booking holds a place; once expired it frees it again
        stale = Booking.objects.create(package=pkg, first_name="x", last_name="y", email="x@y.com", num_guests=1,
                                       travel_date="2027-01-04", unit_price=Decimal("600.00"),
                                       total_amount=Decimal("600.00"), currency="GHS")
        self.assertEqual(pkg.spots_left, 0)
        Booking.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(pkg.spots_left, 1)


@patch("payments.alerts.alert_admin")
@patch("payments.views.verify_webhook_signature", return_value=True)
class DisputeTests(TestCase):
    def _hook(self, event, reference, **data):
        return APIClient().post("/api/payments/webhook/",
                                data=_json.dumps({"event": event, "data": {"transaction": {"reference": reference}, **data}}),
                                content_type="application/json")

    def test_open_dispute_blocks_allocation_until_resolved(self, _sig, alert):
        booking = make_refundable_booking(["1000.00"], days_to_departure=90)   # 90% band
        payment = booking.payments.get()
        r = self._hook("charge.dispute.create", payment.paystack_reference, status="awaiting-merchant-feedback")
        self.assertEqual(r.status_code, 200)
        payment.refresh_from_db()
        self.assertTrue(payment.disputed)
        self.assertEqual(alert.call_count, 1)
        computed = compute_refund(booking)
        self.assertEqual(computed["refund_total"], Decimal("900.00"))          # entitlement unchanged…
        self.assertEqual(computed["allocation"], [])                            # …but nothing may be paid out
        self.assertEqual(computed["breakdown"]["unallocatable"], "900.00")
        self._hook("charge.dispute.resolve", payment.paystack_reference, resolution="merchant-accepted")
        payment.refresh_from_db()
        self.assertFalse(payment.disputed)
        self.assertIn("resolved", payment.dispute_status)
        self.assertEqual(len(compute_refund(booking)["allocation"]), 1)

    def test_processing_a_refund_on_a_disputed_payment_is_refused(self, _sig, _alert):
        booking = make_refundable_booking(["1000.00"], days_to_departure=90)
        (leg,) = create_pending_refunds(booking, reason="x")
        payment = booking.payments.get()
        self._hook("charge.dispute.create", payment.paystack_reference)
        staff = User.objects.create_user(email="fin@test.com", password="x", first_name="F", last_name="N")
        with self.assertRaises(ValueError):
            mark_refund_processed(leg, by_user=staff)

    def test_payments_expose_disputed_flag(self, _sig, _alert):
        booking = make_refundable_booking(["1000.00"])
        self._hook("charge.dispute.create", booking.payments.get().paystack_reference)
        r = APIClient().get(f"/api/bookings/{booking.reference}/")
        self.assertTrue(r.data["payments"][0]["disputed"])


@override_settings(RESEND_API_KEY="test-key", RESEND_FROM_EMAIL="t@test")
class EmailDeliveryLogTests(TestCase):
    @patch("payments.email.resend")
    def test_deliver_logs_sent_with_provider_id(self, mock_resend):
        from payments.email import deliver
        from payments.models import EmailLog
        mock_resend.Emails.send.return_value = {"id": "re_123"}
        booking = make_booking()
        self.assertTrue(deliver("test_kind", booking.email, "Subj", "<p>hi</p>", booking=booking))
        log = EmailLog.objects.get()
        self.assertEqual((log.status, log.provider_id, log.kind, log.booking_id), ("sent", "re_123", "test_kind", booking.id))

    @patch("payments.email.resend")
    def test_deliver_logs_failure_and_never_raises(self, mock_resend):
        from payments.email import deliver
        from payments.models import EmailLog
        mock_resend.Emails.send.side_effect = RuntimeError("boom")
        self.assertFalse(deliver("x", "a@b.com", "S", "<p/>"))
        log = EmailLog.objects.get()
        self.assertEqual((log.status, log.error), ("failed", "boom"))

    @override_settings(RESEND_API_KEY="")
    def test_missing_key_is_logged_not_sent(self):
        from payments.email import deliver
        from payments.models import EmailLog
        self.assertFalse(deliver("x", "a@b.com", "S", "<p/>"))
        self.assertIn("RESEND_API_KEY", EmailLog.objects.get().error)

    @patch("payments.email.deliver")
    def test_failed_payment_emails_guest_once(self, mock_deliver):
        from payments.services import mark_payment_unsuccessful
        booking = make_booking(); p = make_payment(booking)
        with self.captureOnCommitCallbacks(execute=True):
            mark_payment_unsuccessful(p, "failed", {})
            mark_payment_unsuccessful(p, "failed", {})     # idempotent — no second email
        self.assertEqual(mock_deliver.call_count, 1)
        self.assertEqual(mock_deliver.call_args[0][0], "payment_failed")
        self.assertIn(f"/booking/{booking.reference}/pay", mock_deliver.call_args[0][3])

    @patch("payments.email.resend")
    def test_confirmation_lists_what_was_booked_and_policy_versions(self, mock_resend):
        from bookings.models import PolicyAcceptance, PolicyDocument
        from payments.email import send_booking_confirmation
        mock_resend.Emails.send.return_value = {"id": "re_1"}
        booking = make_refundable_booking(["500.00"], total="4700.00",
                                          addons=[{"code": "visa", "name": "Visa on Arrival", "unit_price": "100.00",
                                                   "quantity": 2, "line_total": "200.00", "refundable": False}])
        booking.unit_price = Decimal("2250.00"); booking.save()
        doc = PolicyDocument.objects.create(type="terms", version="1.0", title="Booking Terms", body="t",
                                            is_current=True, published_at=timezone.now())
        PolicyAcceptance.objects.create(booking=booking, document=doc, email=booking.email)
        send_booking_confirmation(booking, booking.payments.get())
        html = mock_resend.Emails.send.call_args[0][0]["html"]
        self.assertIn("What you booked", html)
        self.assertIn("Visa on Arrival", html)
        self.assertIn("Booking Terms v1.0", html)
        self.assertIn("/terms-and-conditions", html)


@patch("payments.email._send")
@patch("payments.email.deliver")
class StaffCancelAndMiscTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        from django.core.management import call_command
        call_command("setup_staff_groups")
        def staff(group, email):
            u = User.objects.create_user(email=email, password="x", first_name="S", last_name="T", is_staff=True)
            u.groups.add(Group.objects.get(name=group))
            c = APIClient(); c.force_authenticate(User.objects.get(pk=u.pk)); return c
        self.viewer = staff("Azura Viewer", "v3@test.com")
        self.ops = staff("Azura Operations", "o3@test.com")
        self.fin = staff("Azura Finance", "f3@test.com")

    def test_organizer_cancel_refunds_in_full_and_is_finance_only(self, *_):
        booking = make_refundable_booking(["1000.00"], days_to_departure=5)      # guest band would be 0%
        url = f"/api/staff/bookings/{booking.reference}/cancel/"
        self.assertEqual(self.ops.post(url, {"mode": "organizer"}, format="json").status_code, 403)
        self.assertEqual(self.fin.post(url, {"mode": "nonsense"}, format="json").status_code, 400)
        r = self.fin.post(url, {"mode": "organizer", "note": "Tour called off"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.cancellation_reason, Booking.CancellationReason.ORGANIZER)
        self.assertEqual(sum(l.amount for l in booking.refunds.all()), Decimal("1000.00"))

    def test_guest_mode_cancel_uses_bands(self, *_):
        booking = make_refundable_booking(["1000.00"], days_to_departure=5)
        r = self.fin.post(f"/api/staff/bookings/{booking.reference}/cancel/", {"mode": "guest"}, format="json")
        self.assertEqual(r.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.refunds.count(), 0)                                # 0% → nothing owed

    def test_email_history_endpoint(self, *_):
        from payments.models import EmailLog
        booking = make_booking()
        EmailLog.objects.create(booking=booking, kind="receipt", to_email=booking.email, subject="S", status="sent", provider_id="re_1")
        r = self.viewer.get(f"/api/staff/bookings/{booking.reference}/emails/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[0]["kind"], "receipt")
        self.assertEqual(APIClient().get(f"/api/staff/bookings/{booking.reference}/emails/").status_code, 401)

    def test_first_installment_payment_cannot_be_below_deposit(self, *_):
        booking = make_booking()
        booking.payment_plan = Booking.PaymentPlan.INSTALLMENT
        booking.deposit_required = Decimal("1000.00"); booking.save()
        r = APIClient().post("/api/payments/initialize/", {"booking_id": str(booking.id), "intent": "custom", "amount": "200.00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least", r.data["detail"])

    def test_fee_notice_reaches_the_checkout_matrix(self, *_):
        from bookings.pricing import _fee_notice
        from payments.models import OpsConfig
        cfg = OpsConfig.get(); cfg.checkout_fee_notice = "All taxes included."; cfg.save()
        self.assertEqual(_fee_notice(), "All taxes included.")

    def test_deletion_request_raises_a_ticket(self, *_):
        u = User.objects.create_user(email="del@test.com", password="x", first_name="D", last_name="E")
        c = APIClient(); c.force_authenticate(u)
        with patch("payments.alerts.alert_admin") as alert:
            r = c.post("/api/auth/delete-request/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(alert.call_count, 1)
        self.assertIn("del@test.com", alert.call_args[0][1])
        self.assertEqual(APIClient().post("/api/auth/delete-request/").status_code, 401)


class NewsletterUnsubscribeTests(TestCase):
    def test_signed_link_unsubscribes_and_bad_token_is_refused(self):
        from newsletter.models import NewsletterSubscriber
        from newsletter.services import unsubscribe_url
        sub = NewsletterSubscriber.objects.create(email="reader@test.com")
        url = unsubscribe_url(sub.email)
        path = url[url.index("/api/"):]
        r = APIClient().get(path)
        self.assertEqual(r.status_code, 200)
        self.assertIn("unsubscribed", r.content.decode().lower())
        sub.refresh_from_db(); self.assertFalse(sub.is_active)
        self.assertEqual(APIClient().get("/api/newsletter/unsubscribe/?token=forged").status_code, 400)


# ── Back-office name correction / traveller replacement ─────────────────────

@patch("payments.email.deliver")
class TravellerChangeTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        from django.core.management import call_command
        call_command("setup_staff_groups")
        def staff(group, email):
            u = User.objects.create_user(email=email, password="x", first_name="S", last_name="T", is_staff=True)
            u.groups.add(Group.objects.get(name=group))
            c = APIClient(); c.force_authenticate(User.objects.get(pk=u.pk)); return c
        self.viewer = staff("Azura Viewer", "v4@test.com")
        self.ops = staff("Azura Operations", "o4@test.com")

    def _booking(self, days=60):
        b = make_booking(status=Booking.Status.CONFIRMED)
        b.travel_date = timezone.now().date() + timedelta(days=days)
        b.user = User.objects.create_user(email=b.email, password="x", first_name="K", last_name="M")
        b.save()
        return b

    def test_correction_keeps_account_and_records_history(self, mail):
        b = self._booking()
        url = f"/api/staff/bookings/{b.reference}/traveller/"
        self.assertEqual(self.viewer.post(url, {"kind": "correction", "first_name": "Kwame", "last_name": "Mensa"}, format="json").status_code, 403)
        r = self.ops.post(url, {"kind": "correction", "first_name": "Kwame", "last_name": "Mensa"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        b.refresh_from_db()
        self.assertEqual((b.first_name, b.last_name), ("Kwame", "Mensa"))
        self.assertIsNotNone(b.user)                                  # same person — account stays
        change = b.traveller_changes[0]
        self.assertEqual((change["kind"], change["from"]["last_name"], change["to"]["last_name"], change["by"]),
                         ("correction", "Mensah", "Mensa", "o4@test.com"))
        self.assertEqual(mail.call_count, 1)                          # only the traveller (same email)

    def test_replacement_detaches_old_account_and_emails_both(self, mail):
        b = self._booking()
        paid_before = b.amount_paid
        r = self.ops.post(f"/api/staff/bookings/{b.reference}/traveller/",
                          {"kind": "replacement", "first_name": "Ama", "last_name": "Owusu", "email": "ama@test.com"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        b.refresh_from_db()
        self.assertEqual(b.email, "ama@test.com")
        self.assertIsNone(b.user)                                     # ownership never follows an email change
        self.assertEqual(b.amount_paid, paid_before)                  # money stays with the booking
        self.assertEqual(mail.call_count, 2)
        recipients = {c[0][1] for c in mail.call_args_list}
        self.assertEqual(recipients, {"ama@test.com", "kwame@example.com"})

    def test_replacement_inside_14_days_needs_a_review_note(self, _m):
        b = self._booking(days=10)
        url = f"/api/staff/bookings/{b.reference}/traveller/"
        body = {"kind": "replacement", "first_name": "Ama", "last_name": "Owusu", "email": "ama@test.com"}
        self.assertEqual(self.ops.post(url, body, format="json").status_code, 400)
        self.assertEqual(self.ops.post(url, {**body, "note": "Approved by manager, hotel confirmed"}, format="json").status_code, 200)

    def test_no_change_is_rejected(self, _m):
        b = self._booking()
        r = self.ops.post(f"/api/staff/bookings/{b.reference}/traveller/",
                          {"kind": "correction", "first_name": b.first_name, "last_name": b.last_name}, format="json")
        self.assertEqual(r.status_code, 400)


# ── Add-on cancellation: 50% fee (owner decision) ───────────────────────────

@patch("payments.email.deliver")
class AddonCancellationTests(TestCase):
    ADDON = {"code": "enzo_vip", "name": "ENZO VIP Table", "unit": "per_person", "unit_price": "100.00",
             "quantity": 2, "line_total": "200.00", "refundable": True}

    def _booking(self, paid):
        b = make_refundable_booking([paid] if paid else [], total="1200.00", addons=[dict(self.ADDON)])
        b.unit_price = Decimal("500.00"); b.save()          # 2 × 500 + 200 = 1200
        return b

    def test_fully_paid_removal_refunds_half(self, mail):
        from bookings.services import remove_addon
        b = self._booking("1200.00")
        out = remove_addon(b, code="enzo_vip", by_email="fin@test.com")
        b.refresh_from_db()
        self.assertEqual((out["charge"], out["fee"], out["refund"]), (Decimal("200.00"), Decimal("100.00"), Decimal("100.00")))
        self.assertEqual(b.total_amount, Decimal("1100.00"))
        self.assertEqual(sum(l.amount for l in b.refunds.all()), Decimal("100.00"))
        codes = [l["code"] for l in b.addons]
        self.assertNotIn("enzo_vip", codes); self.assertIn("enzo_vip_cancellation_fee", codes)
        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(b)["reconciles"])          # receipts still add up
        self.assertEqual(b.addon_changes[0]["fee"], "100.00")
        self.assertEqual(mail.call_args[0][0], "addon_removed")

    def test_partly_paid_removal_just_lowers_the_balance(self, _m):
        from bookings.services import remove_addon
        b = self._booking("300.00")
        remove_addon(b, code="enzo_vip", by_email="fin@test.com")
        b.refresh_from_db()
        self.assertEqual(b.total_amount, Decimal("1100.00"))
        self.assertEqual(b.refunds.count(), 0)
        self.assertEqual(b.balance, Decimal("800.00"))

    def test_guard_rails(self, _m):
        from bookings.services import IllegalTransition, remove_addon
        b = self._booking("1200.00")
        with self.assertRaises(IllegalTransition):
            remove_addon(b, code="nope", by_email="x")
        remove_addon(b, code="enzo_vip", by_email="x")
        b.refresh_from_db()
        with self.assertRaises(IllegalTransition):                       # can't cancel the fee itself
            remove_addon(b, code="enzo_vip_cancellation_fee", by_email="x")

    def test_endpoint_is_finance_only(self, _m):
        from django.contrib.auth.models import Group
        from django.core.management import call_command
        call_command("setup_staff_groups")
        def staff(group, email):
            u = User.objects.create_user(email=email, password="x", first_name="S", last_name="T", is_staff=True)
            u.groups.add(Group.objects.get(name=group)); c = APIClient(); c.force_authenticate(User.objects.get(pk=u.pk)); return c
        ops, fin = staff("Azura Operations", "o5@test.com"), staff("Azura Finance", "f5@test.com")
        b = self._booking("1200.00")
        url = f"/api/staff/bookings/{b.reference}/remove-addon/"
        self.assertEqual(ops.post(url, {"code": "enzo_vip"}, format="json").status_code, 403)
        r = fin.post(url, {"code": "enzo_vip", "note": "Guest asked by phone"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["total_amount"], "1100.00")
        self.assertEqual(r.data["addon_changes"][0]["note"], "Guest asked by phone")


# ── Adding an experience after booking (owner: yes, until 7 days before) ────

@patch("payments.email.deliver")
class AddonAdditionTests(TestCase):
    def setUp(self):
        from packages.models import PackageAddon, PackageAddonGroup
        self.owner = User.objects.create_user(email="own@test.com", password="x", first_name="O", last_name="W")
        self.booking = make_booking(total="1756.00", status=Booking.Status.CONFIRMED)   # 2 guests
        self.booking.user = self.owner
        self.booking.travel_date = timezone.now().date() + timedelta(days=30)
        self.booking.unit_price = Decimal("878.00")        # 2 × 878 = 1756: a coherent booking
        self.booking.addons = []
        self.booking.save()
        # fully paid THROUGH THE LEDGER — amount_paid is always recomputed from payments
        first = make_payment(self.booking, amount=Decimal("1756.00"))
        apply_successful_payment(first, gateway_ok(first))
        self.booking.refresh_from_db()
        pkg = self.booking.package
        hotels = PackageAddonGroup.objects.create(package=pkg, name="Hotel", selection="single")
        experiences = PackageAddonGroup.objects.create(package=pkg, name="Experiences", selection="multi")
        PackageAddon.objects.create(package=pkg, group=hotels, code="hotel_mid", name="Mid hotel", price=Decimal("2000"), unit="per_person")
        PackageAddon.objects.create(package=pkg, group=experiences, code="enzo_vip", name="ENZO VIP Table", price=Decimal("133"), unit="per_person")
        PackageAddon.objects.create(package=pkg, group=experiences, code="massage", name="Waterfall Massage", price=Decimal("107"), unit="per_person")
        self.client_ = APIClient(); self.client_.force_authenticate(self.owner)

    def test_available_lists_experiences_not_hotels(self, _m):
        r = self.client_.get(f"/api/bookings/{self.booking.reference}/addons/available/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["can_add"])
        self.assertEqual([a["code"] for a in r.data["addons"]], ["enzo_vip", "massage"])
        self.assertEqual(r.data["addons"][0]["line_total"], "266.00")           # 133 × 2 guests
        self.assertEqual(r.data["open_until"], self.booking.travel_date - timedelta(days=7))

    def test_closed_inside_seven_days(self, _m):
        self.booking.travel_date = timezone.now().date() + timedelta(days=6); self.booking.save()
        r = self.client_.get(f"/api/bookings/{self.booking.reference}/addons/available/")
        self.assertFalse(r.data["can_add"]); self.assertEqual(r.data["addons"], [])
        p = APIClient().post("/api/payments/initialize/", {"booking_id": str(self.booking.id), "intent": "addon", "addon_code": "enzo_vip"}, format="json")
        self.assertEqual(p.status_code, 400)

    @patch("payments.views.initialize_transaction", return_value={"access_code": "a", "authorization_url": "https://p/x"})
    def test_pay_then_it_joins_the_booking(self, _init, _m):
        # fully paid booking → normally refused, but an add-on purchase is allowed
        r = APIClient().post("/api/payments/initialize/", {"booking_id": str(self.booking.id), "intent": "addon", "addon_code": "enzo_vip"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["amount"], "266.00")
        payment = Payment.objects.get(paystack_reference=r.data["reference"])
        self.assertEqual((payment.purpose, payment.addon_code), ("addon", "enzo_vip"))
        self.assertNotIn("enzo_vip", [l["code"] for l in Booking.objects.get(pk=self.booking.pk).addons])   # not yet

        with self.captureOnCommitCallbacks(execute=True):
            result = apply_successful_payment(payment, gateway_ok(payment))
        self.assertTrue(result.applied); self.assertEqual(result.problems, [])
        b = Booking.objects.get(pk=self.booking.pk)
        self.assertEqual([l["code"] for l in b.addons], ["enzo_vip"])
        self.assertEqual(b.total_amount, Decimal("2022.00"))                    # 1756 + 266
        self.assertEqual(b.amount_paid, Decimal("2022.00"))
        self.assertEqual(b.balance, Decimal("0.00"))
        self.assertFalse(Payment.objects.get(pk=payment.pk).needs_review)         # not an "over-payment"
        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(b)["reconciles"])
        # already on the booking → can't be added twice
        r2 = self.client_.get(f"/api/bookings/{self.booking.reference}/addons/available/")
        self.assertEqual([a["code"] for a in r2.data["addons"]], ["massage"])

    def test_unknown_or_hotel_code_refused(self, _m):
        for code in ("hotel_mid", "nope"):
            r = APIClient().post("/api/payments/initialize/", {"booking_id": str(self.booking.id), "intent": "addon", "addon_code": code}, format="json")
            self.assertEqual(r.status_code, 400, code)


class HotelCancellationTests(TestCase):
    @patch("payments.email.deliver")
    def test_hotel_cancellation_keeps_half_and_falls_back_to_no_hotel(self, _m):
        from bookings.services import remove_addon
        from packages.models import PackageAddon, PackageAddonGroup
        hotel = {"code": "hotel_mid", "name": "Mid-Tier Accra Hotel", "unit": "per_person", "unit_price": "2000.00",
                 "quantity": 2, "line_total": "4000.00", "refundable": True}
        b = make_refundable_booking(["5756.00"], total="5756.00", addons=[hotel])   # 2 × 878 + 4000
        b.unit_price = Decimal("878.00"); b.save()
        g = PackageAddonGroup.objects.create(package=b.package, name="Accra Accommodation", selection="single")
        PackageAddon.objects.create(package=b.package, group=g, code="no_hotel", name="No Accra Hotel", price=0, unit="per_booking", is_default=True)
        PackageAddon.objects.create(package=b.package, group=g, code="hotel_mid", name="Mid-Tier Accra Hotel", price=2000, unit="per_person")

        out = remove_addon(b, code="hotel_mid", by_email="fin@test.com")
        b.refresh_from_db()
        self.assertEqual((out["fee"], out["refund"]), (Decimal("2000.00"), Decimal("2000.00")))
        self.assertEqual(b.total_amount, Decimal("3756.00"))                     # 1756 + 2000 fee
        codes = [l["code"] for l in b.addons]
        self.assertEqual(codes, ["no_hotel", "hotel_mid_cancellation_fee"])
        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(b)["reconciles"])
