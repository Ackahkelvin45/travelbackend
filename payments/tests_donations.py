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
