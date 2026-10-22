from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from bookings import tests as pricing_tests
from payments.models import Payment
from payments.services import apply_successful_payment

from .models import Booking


class AfterBookingTests(TestCase):
    """Change Request v4: event-start access, add-ons from the guest dashboard,
    and balance reminders 30 / 14 / 7 days (+ final) before the event."""

    setUpTestData = pricing_tests.ComponentPricingTests.__dict__["setUpTestData"]
    checkout = pricing_tests.ComponentPricingTests.checkout

    def booked(self, pay=None, **kw):
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="group", n=3, rooms=[{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}], **kw)
        if pay:
            self.pay(booking, pay)
        return booking

    def pay(self, booking, amount, **kw):
        p = Payment.objects.create(booking=booking, paystack_reference=f"T-{Payment.objects.count()}",
                                   amount=Decimal(amount), currency="USD", **kw)
        apply_successful_payment(p, None)
        booking.refresh_from_db()
        return p

    # ── Event access ──────────────────────────────────────────────────────────

    def test_unpaid_guests_cannot_check_in_without_an_exception(self):
        from .services import IllegalTransition, check_in_ticket, forfeit_ticket
        staff = get_user_model().objects.create_superuser(email="ops@example.com", password="x-Strong-pass-1",
                                                          first_name="A", last_name="D")
        booking = self.booked(pay="2599.00", pay_separately=True)  # organiser paid; two guests owe 899
        lead, member, other = booking.travellers.order_by("position")

        check_in_ticket(lead, by=staff)
        self.assertIsNotNone(lead.checked_in_at)
        with self.assertRaises(IllegalTransition):
            check_in_ticket(member, by=staff)

        member.access_exception, member.access_note = True, "Paying cash on arrival, approved by Efua"
        member.save()
        check_in_ticket(member, by=staff)

        forfeit_ticket(other)
        self.assertIsNotNone(other.forfeited_at)
        with self.assertRaises(IllegalTransition):
            check_in_ticket(other, by=staff)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)  # nothing cancelled automatically

        self.client.force_login(staff)
        res = self.client.get("/admin/bookings/eventticket/?access=unpaid")
        self.assertEqual(res.status_code, 200)

    # ── Add-ons from the guest dashboard ──────────────────────────────────────

    def test_add_experience_and_transfer_after_booking(self):
        from .services import available_component_addons, quote_component_addon, IllegalTransition
        booking = self.booked(pay="4397.00")  # 3 × 899 + 1,700, paid in full
        lead, member, _ = booking.travellers.order_by("position")

        codes = {o["code"] for o in available_component_addons(booking)}
        self.assertEqual(codes, {"enzo_vip", "waterfall_massage", "airport_pickup", "airport_return"})
        with self.assertRaises(IllegalTransition):
            quote_component_addon(booking, "enzo_vip", [lead.id])  # 18+ not confirmed

        line = quote_component_addon(booking, "airport_pickup", [lead.id, member.id])
        self.assertEqual((line["quantity"], line["line_total"]), (1, "40.00"))  # one vehicle for two

        with mock.patch("payments.views.initialize_transaction", return_value={"access_code": "a", "authorization_url": "u"}):
            res = self.client.post("/api/payments/initialize/", {
                "booking_id": str(booking.id), "intent": "addon", "addon_code": "enzo_vip",
                "traveller_ids": [str(lead.id)], "adult": True,
            }, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        payment = Payment.objects.get(paystack_reference=res.json()["reference"])
        payment.status = Payment.Status.PENDING
        payment.save()
        apply_successful_payment(payment, None)
        booking.refresh_from_db()
        lead.refresh_from_db()

        self.assertEqual(booking.total_amount, Decimal("4617.00"))
        self.assertTrue(booking.is_paid)
        self.assertIn("enzo_vip", lead.experiences)
        self.assertEqual([str(o) for *_, o in booking.traveller_balances()], ["0.00", "0.00", "0.00"])
        self.assertNotIn("enzo_vip", {o["code"] for o in available_component_addons(booking)
                                      if str(lead.id) in o["eligible_travellers"]})

    # ── Reminders ─────────────────────────────────────────────────────────────

    def run_reminders(self, today):
        with mock.patch("bookings.management.commands.send_payment_reminders.timezone") as tz, \
                mock.patch("payments.email.deliver", return_value=True) as deliver:
            tz.now.return_value = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
            call_command("send_payment_reminders", stdout=StringIO())
        return [c.args[0] for c in deliver.call_args_list]

    def test_trip_reminders_30_14_7_and_final(self):
        self.booked(pay="2599.00", pay_separately=True)
        start = date(2027, 1, 6)
        sent = []
        for days in (40, 30, 29, 14, 10, 7, 3, 1):
            sent += self.run_reminders(start - timedelta(days=days))
        self.assertEqual(sent, ["reminder_30d", "reminder_14d", "reminder_7d", "reminder_1d"])

    def test_member_paying_their_share_keeps_the_organisers_balance(self):
        """Regression (found in the Paystack end-to-end run): a member's own
        payment must not shift the organiser's room deposit cover to someone else."""
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        booking = self.checkout(token, mode="group", n=3, pay_separately=True,
                                rooms=[{"stay_type": "airbnb_2bed", "occupants": [0, 1, 2]}])
        Booking.objects.filter(pk=booking.pk).update(payment_plan="installment")
        booking.refresh_from_db()
        lead, member, other = booking.travellers.order_by("position")
        # Organiser covered their own Core + half their room share and the other two's room deposits.
        for t, portion in ((lead, "1182.33"), (member, "283.33"), (other, "283.33")):
            t.checkout_portion = Decimal(portion)
            t.save()
        self.pay(booking, "1748.99")  # the three portions
        self.pay(booking, str(member.share - Decimal("283.33")), traveller=member)
        owed = {t.full_name: o for t, _, _, o in booking.traveller_balances()}
        self.assertEqual(owed[member.full_name], Decimal("0.00"))
        self.assertEqual(owed[lead.full_name], lead.share - Decimal("1182.33"))
