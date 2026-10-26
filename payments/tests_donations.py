from decimal import Decimal
from unittest import mock

from django.test import TestCase

from .models import Donation, OpsConfig


class DonationTests(TestCase):
    """Academy donations (Change Request v4 §7): closed until the subaccount
    is set, settled to it, verified before recording, never a booking payment."""

    body = {"name": "Efua Mensah", "email": "efua@example.com", "amount": "25.00"}

    def open_donations(self):
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={
            "donations_enabled": True, "donation_subaccount": "ACCT_academy"})

    def test_closed_until_the_academy_account_is_set(self):
        res = self.client.post("/api/donations/", self.body, content_type="application/json")
        self.assertEqual(res.status_code, 503)
        self.assertFalse(Donation.objects.exists())

    @mock.patch("payments.donations._charge_rate", return_value=(Decimal("15.5"), "test"))
    def test_donation_settles_to_the_subaccount_and_is_verified(self, _rate):
        self.open_donations()
        with mock.patch("payments.paystack._post", return_value={"access_code": "a", "authorization_url": "u"}) as post:
            res = self.client.post("/api/donations/", self.body, content_type="application/json")
        self.assertEqual(res.status_code, 201, res.content)
        payload = post.call_args.args[1]
        self.assertEqual((payload["subaccount"], payload["bearer"], payload["amount"]),
                         ("ACCT_academy", "subaccount", 38750))  # USD 25 × 15.5 = GHS 387.50

        ref = res.json()["reference"]
        wrong = {"status": "success", "amount": 100, "currency": "GHS"}
        with mock.patch("payments.paystack.verify_transaction", return_value=wrong):
            self.assertEqual(self.client.get(f"/api/donations/verify/{ref}/").json()["status"], "pending")
        self.assertTrue(Donation.objects.get().needs_review)

        right = {"status": "success", "amount": 38750, "currency": "GHS"}
        with mock.patch("payments.paystack.verify_transaction", return_value=right), \
                mock.patch("payments.email.deliver", return_value=True) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.get(f"/api/donations/verify/{ref}/").json()["status"], "success")
        self.assertEqual(deliver.call_args.args[0], "donation_receipt")
        self.assertIsNotNone(Donation.objects.get().receipt_sent_at)

    def test_checkout_percentage_needs_a_booking(self):
        self.open_donations()
        res = self.client.post("/api/donations/", {**self.body, "amount": None, "percent": 5},
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_checkout_donation_is_added_to_the_first_payment_and_split(self):
        """$5,000 trip + 10% = $5,500 charged once: Azura keeps 5,000, the
        Academy subaccount gets 500; the booking ledger only ever sees 5,000."""
        from payments.models import Payment
        from payments.services import apply_successful_payment
        from payments.tests import make_booking
        self.open_donations()
        booking = make_booking(total="5000.00")
        init = {"booking_id": str(booking.pk), "donation_percent": 10}
        with mock.patch("payments.views.initialize_transaction",
                        return_value={"access_code": "a", "authorization_url": "u"}) as gw:
            self.assertEqual(self.client.post("/api/payments/initialize/", {**init, "donation_percent": 15},
                                              content_type="application/json").status_code, 400)  # 5, 10 or 20
            res = self.client.post("/api/payments/initialize/", init, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        kw = gw.call_args.kwargs
        self.assertEqual((kw["amount_kobo"], kw["subaccount"], kw["transaction_charge"]),
                         (550000, "ACCT_academy", 500000))
        payment = Payment.objects.get(paystack_reference=res.json()["reference"])
        self.assertEqual(payment.amount, Decimal("5000.00"))
        donation = Donation.objects.get(payment=payment)
        self.assertEqual((donation.amount, donation.percent), (Decimal("500.00"), 10))

        with mock.patch("payments.email.deliver", return_value=True) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                apply_successful_payment(payment, {"amount": 550000, "currency": "GHS", "status": "success"})
        sent = {c.args[0]: c.args[3] for c in deliver.call_args_list}
        self.assertIn("donation_receipt", sent)
        self.assertIn("Academy donation (10%)", sent["confirmation"])
        self.assertIn("GHS 5,500.00", sent["confirmation"])  # matches the card statement
        booking.refresh_from_db()
        donation.refresh_from_db()
        self.assertEqual((booking.amount_paid, booking.total_amount), (Decimal("5000.00"), Decimal("5000.00")))
        self.assertEqual(donation.status, Donation.Status.SUCCESS)

        # Only on the first payment.
        with mock.patch("payments.views.initialize_transaction", return_value={}):
            res = self.client.post("/api/payments/initialize/", init, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_checkout_donation_without_the_split_amount_is_not_applied(self):
        from payments.models import Payment
        from payments.services import apply_successful_payment
        from payments.tests import make_booking
        self.open_donations()
        booking = make_booking(total="1000.00")
        with mock.patch("payments.views.initialize_transaction", return_value={}):
            res = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.pk), "donation_percent": 5},
                                   content_type="application/json")
        payment = Payment.objects.get(paystack_reference=res.json()["reference"])
        with mock.patch("payments.alerts.alert_admin"):
            result = apply_successful_payment(payment, {"amount": 100000, "currency": "GHS"})  # donation missing
        self.assertEqual(result.problems, ["gateway_mismatch"])
        self.assertEqual(Donation.objects.get().status, Donation.Status.PENDING)


class WhatsAppConfirmationTests(TestCase):
    """Opt-in WhatsApp template after a verified payment (Change Request v4 §8)."""

    def booking(self, phone="+233 24 555 0101", opted=True):
        from payments.tests import make_booking
        b = make_booking()
        b.phone, b.whatsapp_updates = phone, opted
        b.save()
        return b

    def test_number_needs_a_country_code(self):
        from .whatsapp_notify import _wa_id
        self.assertEqual(_wa_id("+233 24 555 0101"), "233245550101")
        self.assertEqual(_wa_id("00233245550101"), "233245550101")
        self.assertEqual(_wa_id("024 555 0101"), "")   # local: skipped, never guessed
        self.assertEqual(_wa_id(""), "")

    def test_sends_template_only_when_opted_in_and_configured(self):
        from django.test import override_settings
        from .whatsapp_notify import send_payment_whatsapp
        b = self.booking()
        with mock.patch("payments.whatsapp_notify.requests.post") as post:
            self.assertFalse(send_payment_whatsapp(b))      # no template yet: quietly nothing
            post.assert_not_called()
            OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={"whatsapp_confirmation_template": "payment_received"})
            with override_settings(WHATSAPP_PHONE_NUMBER_ID="123", WHATSAPP_TOKEN="t"):
                self.assertTrue(send_payment_whatsapp(b))
                self.assertFalse(send_payment_whatsapp(self.booking(opted=False)))
                self.assertFalse(send_payment_whatsapp(self.booking(phone="0245550101")))
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["to"], body["type"], body["template"]["name"]), ("233245550101", "template", "payment_received"))
        self.assertEqual(body["template"]["components"][0]["parameters"][1]["text"], b.reference)
        self.assertEqual(post.call_count, 1)

    def test_failure_never_blocks_payment(self):
        import requests
        from django.test import override_settings
        from .services import apply_successful_payment
        from .models import Payment
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={"whatsapp_confirmation_template": "payment_received"})
        b = self.booking()
        p = Payment.objects.create(booking=b, paystack_reference="WA-1", amount=b.total_amount, currency=b.currency)
        with override_settings(WHATSAPP_PHONE_NUMBER_ID="123", WHATSAPP_TOKEN="t"), \
                mock.patch("payments.whatsapp_notify.requests.post", side_effect=requests.ConnectionError), \
                mock.patch("payments.email.deliver", return_value=True), self.captureOnCommitCallbacks(execute=True):
            result = apply_successful_payment(p, None)
        b.refresh_from_db()
        self.assertTrue(result.applied)
        self.assertEqual(b.status, "confirmed")
