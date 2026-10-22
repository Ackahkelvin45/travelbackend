from decimal import Decimal
from unittest import mock

from django.test import TestCase

from bookings import tests as pricing_tests


class GroupCheckoutPaymentTests(TestCase):
    """The checkout payment of a pay-separately group charges the organiser's
    share plus every room deposit (intent=deposit), even on the full plan."""

    setUpTestData = pricing_tests.ComponentPricingTests.__dict__["setUpTestData"]
    checkout = pricing_tests.ComponentPricingTests.checkout

    def test_deposit_intent_charges_the_checkout_portion(self):
        from bookings.services import create_hold

        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="group", n=3, pay_separately=True,
                                rooms=[{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}])
        with mock.patch("payments.views.initialize_transaction",
                        return_value={"access_code": "a", "authorization_url": "u"}):
            res = self.client.post("/api/payments/initialize/", {"booking_id": str(booking.id), "intent": "deposit"},
                                   content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(Decimal(res.json()["amount"]), Decimal("2599.00"))
