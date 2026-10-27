"""
Exchange-rate money checks: across many rates, margins, totals and payment
splits, the guest is charged exactly amount × the stored rate, the USD ledger
lands exactly on the total, nothing a pesewa off is ever credited, a missed
webhook can't strand a payment, and every charge records where its rate
came from.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import FxRate, Payment
from .money import quantize, to_subunits
from .refunds import gateway_share
from .services import apply_successful_payment, reconcile_open_payments
from .tests import make_booking, provider_ok


def usd_booking(total, margin="1.50", manual=None):
    booking = make_booking(total=total)
    booking.currency = "USD"
    booking.save()
    package = booking.package
    package.currency, package.fx_margin_percent, package.charge_exchange_rate = "USD", Decimal(margin), manual
    package.save()
    return booking


def gateway_echo(mock_init, **extra):
    """What Paystack reports for the charge we just initialized."""
    kwargs = mock_init.call_args.kwargs
    return {"status": "success", "amount": kwargs["amount_kobo"], "currency": kwargs["currency"], **extra}


@patch("payments.views.initialize_transaction", return_value={"access_code": "a", "authorization_url": "https://p/x"})
@patch("payments.fx.requests.get")
class FxChargeAccuracyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def pay(self, booking, mock_init, **body):
        r = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id), **body}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        payment = Payment.objects.get(paystack_reference=r.data["reference"])
        return r, payment

    def test_every_rate_margin_total_and_split_charges_exactly(self, mock_get, mock_init):
        for market in ("5.1234", "11.6714", "15.5", "59.99"):
            for margin in ("0", "1.50", "2.75"):
                for total in ("1.00", "999.99", "3098.00", "12345.67"):
                    for splits in ((), ("0.33",), ("0.10", "0.45")):
                        with self.subTest(market=market, margin=margin, total=total, splits=splits):
                            cache.clear()
                            FxRate.objects.all().delete()
                            mock_get.return_value = provider_ok(float(market))
                            booking = usd_booking(total, margin)
                            charged_ghs = Decimal("0")
                            for part in splits:
                                amount = quantize(Decimal(total) * Decimal(part))
                                if amount <= 0:
                                    continue
                                r, p = self.pay(booking, mock_init, intent="custom", amount=str(amount))
                                charged_ghs += self.check_and_apply(r, p, mock_init)
                            r, p = self.pay(booking, mock_init)  # the balance
                            charged_ghs += self.check_and_apply(r, p, mock_init)

                            booking.refresh_from_db()
                            self.assertEqual(booking.amount_paid, Decimal(total))   # USD ledger exact
                            self.assertEqual(booking.balance, Decimal("0.00"))
                            self.assertEqual(booking.status, "confirmed")
                            rate = (Decimal(market) * (1 + Decimal(margin) / 100)).quantize(Decimal("0.0001"))
                            # Never more than one pesewa of rounding per charge, either way.
                            self.assertLessEqual(abs(charged_ghs - Decimal(total) * rate),
                                                 Decimal("0.005") * (len(splits) + 1))

    def check_and_apply(self, r, payment, mock_init):
        # The stored rate reproduces the charge exactly, and it's what Paystack was sent.
        self.assertEqual(payment.charged_amount, quantize(payment.amount * payment.exchange_rate))
        self.assertEqual(r.data["exchange_rate"], str(payment.exchange_rate))
        self.assertEqual(mock_init.call_args.kwargs["amount_kobo"], to_subunits(payment.charged_amount))
        self.assertEqual(mock_init.call_args.kwargs["currency"], "GHS")
        self.assertTrue(payment.exchange_rate_source)
        self.assertTrue(apply_successful_payment(payment, gateway_echo(mock_init)).applied)
        return payment.charged_amount

    def test_rate_moving_after_checkout_still_credits_what_was_charged(self, mock_get, mock_init):
        mock_get.return_value = provider_ok(11.6714)
        booking = usd_booking("3098.00")
        r, payment = self.pay(booking, mock_init, intent="custom", amount="1000.00")
        charged = gateway_echo(mock_init)

        cache.clear()  # the hourly refresh lands mid-checkout at a new rate
        mock_get.return_value = provider_ok(12.4)
        self.assertTrue(apply_successful_payment(payment, charged).applied)

        r, balance = self.pay(booking, mock_init)
        self.assertEqual(balance.amount, Decimal("2098.00"))
        self.assertEqual(balance.exchange_rate, Decimal("12.5860"))  # 12.4 × 1.015, the new rate
        apply_successful_payment(balance, gateway_echo(mock_init))
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("3098.00"))
        self.assertTrue(booking.is_paid)

    @patch("payments.services.alert_admin")
    def test_a_pesewa_off_either_way_is_never_credited(self, mock_alert, mock_get, mock_init):
        mock_get.return_value = provider_ok(11.6714)
        for delta in (-1, 1):
            with self.subTest(delta=delta):
                cache.clear()
                booking = usd_booking("3098.00")
                r, payment = self.pay(booking, mock_init)
                data = gateway_echo(mock_init)
                data["amount"] += delta
                result = apply_successful_payment(payment, data)
                payment.refresh_from_db()
                booking.refresh_from_db()
                self.assertIn("gateway_mismatch", result.problems)
                self.assertEqual((payment.status, payment.needs_review), ("pending", True))
                self.assertIn(str(data["amount"]), payment.review_note)  # what Paystack said, for debugging
                self.assertEqual((booking.amount_paid, booking.status), (Decimal("0.00"), "pending"))

    def test_fee_passed_to_the_guest_still_applies(self, mock_get, mock_init):
        mock_get.return_value = provider_ok(11.6714)
        booking = usd_booking("3098.00")
        r, payment = self.pay(booking, mock_init)
        expected = mock_init.call_args.kwargs["amount_kobo"]
        result = apply_successful_payment(payment, gateway_echo(
            mock_init, amount=expected + 71500, requested_amount=expected))
        self.assertTrue(result.applied)
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("3098.00"))  # the ledger never takes the fee

    def test_every_charge_records_where_its_rate_came_from(self, mock_get, mock_init):
        mock_get.return_value = provider_ok(11.6714)
        booking = usd_booking("3098.00", manual=Decimal("12.0000"))
        sources = []
        for setup in (lambda: None, lambda: None, "provider_down", "stale"):
            if setup == "provider_down":
                cache.clear()
                mock_get.side_effect = requests.ConnectionError("down")
            if setup == "stale":
                cache.clear()
                FxRate.objects.update(fetched_at=timezone.now() - timedelta(hours=48))
            r, p = self.pay(booking, mock_init, intent="custom", amount="10.00")
            p.refresh_from_db()
            sources.append((p.exchange_rate_source, p.exchange_rate))
        self.assertEqual(sources, [
            ("open.er-api.com", Decimal("11.8465")),
            ("open.er-api.com (cached)", Decimal("11.8465")),
            ("db-fallback", Decimal("11.8465")),
            ("manual-fallback", Decimal("12.0000")),
        ])

    @patch("payments.views.alert_admin", create=True)
    def test_no_rate_means_no_payment_and_no_charge(self, _alert, mock_get, mock_init):
        mock_get.side_effect = requests.ConnectionError("down")
        booking = usd_booking("3098.00")
        r = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id)}, format="json")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(booking.payments.exists())
        mock_init.assert_not_called()


@patch("payments.services.alert_admin")
@patch("payments.paystack.verify_transaction")
class ReconcileTests(TestCase):
    """A paid charge whose webhook never arrived is found and applied."""

    def open_payment(self, minutes_ago=30, **fields):
        booking = make_booking(total="500.00")
        payment = Payment.objects.create(booking=booking, paystack_reference=f"AZT-PAY-{booking.reference}-R",
                                         amount=Decimal("500.00"), currency="GHS", **fields)
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return payment

    def test_missed_webhook_is_applied_and_reported(self, mock_verify, mock_alert):
        p = self.open_payment()
        retired = self.open_payment(status=Payment.Status.ABANDONED)  # retired by a newer attempt, then paid
        mock_verify.return_value = {"status": "success", "amount": 50000, "currency": "GHS"}
        self.assertIn("applied 2", reconcile_open_payments())
        for payment in (p, retired):
            payment.refresh_from_db()
            self.assertEqual(payment.status, "success")
            self.assertEqual(payment.booking.amount_paid, Decimal("500.00"))
        mock_alert.assert_called_once()
        self.assertEqual(reconcile_open_payments(), "checked 0, applied 0, failed 0, errors 0")  # idempotent

    def test_failed_and_still_open_charges(self, mock_verify, mock_alert):
        failed, ongoing = self.open_payment(), self.open_payment()
        mock_verify.side_effect = lambda ref: {"status": "failed" if ref == failed.paystack_reference else "ongoing"}
        reconcile_open_payments()
        failed.refresh_from_db()
        ongoing.refresh_from_db()
        self.assertEqual((failed.status, ongoing.status), ("failed", "pending"))
        mock_alert.assert_not_called()

    def test_leaves_alone_what_it_should(self, mock_verify, mock_alert):
        self.open_payment(minutes_ago=2)              # guest may still be paying
        self.open_payment(minutes_ago=60 * 24 * 4)    # too old to keep polling
        self.open_payment(needs_review=True)          # staff are on it
        self.open_payment(status=Payment.Status.SUCCESS)
        self.assertEqual(reconcile_open_payments(), "checked 0, applied 0, failed 0, errors 0")
        mock_verify.assert_not_called()

    def test_gateway_error_is_counted_not_raised(self, mock_verify, mock_alert):
        p = self.open_payment()
        mock_verify.side_effect = requests.ConnectionError("paystack down")
        self.assertIn("errors 1", reconcile_open_payments())
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")


class RefundRoundingTests(TestCase):
    def test_refund_legs_on_one_charge_add_up_exactly(self):
        p = Payment(amount=Decimal("3098.00"), charged_amount=Decimal("36700.49"))
        for legs in ([Decimal("1549.00")] * 2, [Decimal("442.57")] * 6 + [Decimal("442.58")],
                     [Decimal("0.01")] * 3 + [Decimal("3097.97")]):
            with self.subTest(legs=legs):
                done, ghs = Decimal("0"), Decimal("0")
                for leg in legs:
                    ghs += gateway_share(p, done, leg)
                    done += leg
                self.assertEqual(ghs, p.charged_amount)
        tiny = Payment(amount=Decimal("2.00"), charged_amount=Decimal("100.01"))
        self.assertEqual(gateway_share(tiny, 0, Decimal("1.00")) + gateway_share(tiny, Decimal("1.00"), Decimal("1.00")),
                         Decimal("100.01"))  # per-leg rounding would refund 100.02
