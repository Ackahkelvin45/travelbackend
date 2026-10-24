from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from bookings import tests as pricing_tests
from payments.models import OpsConfig, Payment
from payments.services import apply_successful_payment

from .models import Booking, Lead


class CrmFollowupTests(TestCase):
    """Phase 5: unfinished checkouts become leads; quote reminders only with
    consent and only until the guest pays or opts out; staff SLA alerts once."""

    setUpTestData = pricing_tests.ComponentPricingTests.__dict__["setUpTestData"]
    checkout = pricing_tests.ComponentPricingTests.checkout

    def setUp(self):
        OpsConfig.objects.update_or_create(pk=OpsConfig.get().pk, defaults={
            "reminder_schedule_hours": [24, 72], "alert_email": "ops@example.com"})

    def unpaid(self, consent=True, hours_ago=2):
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token, marketing_consent=consent)
        Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(hours=hours_ago))
        return booking

    def test_unfinished_checkout_becomes_a_lead_once(self):
        from .crm import checkout_leads
        booking = self.unpaid()
        self.assertEqual(checkout_leads(), 1)
        self.assertEqual(checkout_leads(), 0)
        lead = Lead.objects.get()
        self.assertEqual((lead.source, lead.email, lead.quote["total"]), ("checkout", "ama@example.com", "2898.00"))
        self.assertEqual(lead.quote["abandoned_booking"], booking.reference)

        recent = self.unpaid(hours_ago=0)  # still inside the hour: not abandoned yet
        Lead.objects.all().delete()
        Booking.objects.exclude(pk=recent.pk).delete()
        self.assertEqual(checkout_leads(), 0)

    def test_reminders_need_consent_and_stop_after_payment(self):
        from .crm import checkout_leads, quote_reminders
        booking = self.unpaid(consent=True)
        checkout_leads()
        lead = Lead.objects.get()
        now = lead.created_at
        with mock.patch("payments.email.deliver", return_value=True) as deliver:
            self.assertEqual(quote_reminders(now + timedelta(hours=1)), 0)    # not due yet
            self.assertEqual(quote_reminders(now + timedelta(hours=25)), 1)   # 24 h reminder
            self.assertEqual(quote_reminders(now + timedelta(hours=30)), 0)   # already sent
            self.assertIn("Unsubscribe", deliver.call_args.args[3])

            p = Payment.objects.create(booking=booking, paystack_reference="T-1", amount=booking.total_amount,
                                       currency="USD")
            apply_successful_payment(p, None)
            lead.refresh_from_db()
            self.assertEqual(lead.stage, Lead.Stage.WON)
            self.assertEqual(quote_reminders(now + timedelta(hours=73)), 0)  # paid: no 72 h reminder
            self.assertEqual(deliver.call_count, 1)

        Lead.objects.all().delete()
        self.unpaid(consent=False)
        checkout_leads()
        with mock.patch("payments.email.deliver", return_value=True) as deliver:
            self.assertEqual(quote_reminders(timezone.now() + timedelta(hours=100)), 0)
        deliver.assert_not_called()

    def test_unsubscribe_link_stops_reminders(self):
        from .crm import checkout_leads, quote_reminders, unsubscribe_url
        self.unpaid()
        checkout_leads()
        lead = Lead.objects.get()
        token = unsubscribe_url(lead).split("token=")[1]
        self.assertEqual(self.client.post("/api/leads/unsubscribe/", {"token": "bad"}).status_code, 400)
        self.assertEqual(self.client.post("/api/leads/unsubscribe/", {"token": token}).status_code, 200)
        lead.refresh_from_db()
        self.assertFalse(lead.marketing_consent)
        with mock.patch("payments.email.deliver", return_value=True) as deliver:
            quote_reminders(lead.created_at + timedelta(hours=100))
        deliver.assert_not_called()

    def test_staff_alerted_once_for_late_reply_and_missed_contact(self):
        from .crm import staff_alerts
        Lead.objects.create(name="Kofi", email="kofi@example.com", source=Lead.Source.CHAT,
                            reply_due_at=timezone.now() - timedelta(minutes=5))
        booking = self.unpaid()
        p = Payment.objects.create(booking=booking, paystack_reference="T-2", amount=booking.total_amount, currency="USD")
        apply_successful_payment(p, None)
        Payment.objects.filter(pk=p.pk).update(paid_at=timezone.now() - timedelta(hours=30))
        with mock.patch("payments.email.deliver", return_value=True) as deliver, \
                override_settings(FRONTEND_URL="https://azuratravels.live", BACKEND_URL="https://azuratravelsbackend.cc"):
            self.assertEqual(staff_alerts(), 2)
            self.assertEqual(staff_alerts(), 0)
        subjects = sorted(c.args[2] for c in deliver.call_args_list)
        self.assertEqual(subjects, [f"[Azura] Guest not contacted: {booking.reference}", "[Azura] Lead reply overdue: Kofi"])
        bodies = {c.args[2]: c.args[3] for c in deliver.call_args_list}
        lead_body = bodies["[Azura] Lead reply overdue: Kofi"]
        for text in ("kofi@example.com", "Reply due", "/admin/bookings/lead/", "azuratravels.live"):
            self.assertIn(text, lead_body)
        contact_body = bodies[f"[Azura] Guest not contacted: {booking.reference}"]
        for text in (booking.email, "Balance", f"/admin/bookings/booking/{booking.pk}/change/"):
            self.assertIn(text, contact_body)
        self.assertNotIn("Questions? Reply", contact_body)  # internal: no guest footer
        self.assertEqual({c.args[1] for c in deliver.call_args_list}, {"ops@example.com"})

    def test_staging_alerts_say_staging(self):
        from payments.email import site_label
        with override_settings(FRONTEND_URL="https://staging.azuratravels.live"):
            self.assertEqual(site_label(), "STAGING")
        with override_settings(FRONTEND_URL="http://localhost:3000"):
            self.assertEqual(site_label(), "LOCAL")
        with override_settings(FRONTEND_URL="https://azuratravels.live"):
            self.assertEqual(site_label(), "")
