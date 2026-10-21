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
