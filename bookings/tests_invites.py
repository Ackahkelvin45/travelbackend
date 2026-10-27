"""Guest invites, per-guest dashboards and linked group bookings (client feedback, Oct 2026)."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from . import tests as base  # module import: importing the class would re-run its tests
from .models import Booking, BookingTraveller
from .services import create_hold


class GuestInviteTests(TestCase):
    setUpTestData = classmethod(base.ComponentPricingTests.setUpTestData.__func__)
    checkout = base.ComponentPricingTests.checkout

    def setUp(self):
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        self.booking = self.checkout(token)
        User = get_user_model()
        self.lead = User.objects.create_user(email="ama@example.com", password="x-Strong-pass-1", first_name="Ama", last_name="O")
        Booking.objects.filter(pk=self.booking.pk).update(user=self.lead)
        self.guest = self.booking.travellers.get(is_lead=False)
        self.lead_api = APIClient()
        self.lead_api.force_authenticate(self.lead)

    def invite(self, email="kojo@example.com"):
        with mock.patch("payments.email.deliver", return_value=True) as deliver:
            res = self.lead_api.post(f"/api/bookings/{self.booking.reference}/travellers/{self.guest.id}/invite/",
                                     {"email": email}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.guest.refresh_from_db()
        return deliver, self.guest.invite_token

    def test_invite_complete_and_status_without_new_tickets(self):
        deliver, token = self.invite()
        kind, to, _subject, html_body = deliver.call_args.args
        self.assertEqual((kind, to), ("guest_invite", "kojo@example.com"))
        self.assertIn(f"/invite/{token}", html_body)
        rows = {t["id"]: t for t in self.lead_api.get(f"/api/bookings/{self.booking.reference}/").json()["travellers"]}
        self.assertEqual(rows[str(self.guest.id)]["invite_status"], "pending")

        page = self.client.get(f"/api/bookings/invite/{token}/").json()
        self.assertEqual(page["guest"]["full_name"], "G1")
        self.assertEqual(page["ticket"], f"{self.booking.reference}-T2")
        self.assertNotIn("G0", str(page))  # never another guest's details

        res = self.client.put(f"/api/bookings/invite/{token}/", {
            "full_name": "Kojo Owusu", "phone": "+233200000000", "arrival_flight": "BA81",
        }, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(BookingTraveller.objects.filter(booking=self.booking).count(), 2)  # same ticket, updated
        self.guest.refresh_from_db()
        self.assertEqual((self.guest.full_name, self.guest.arrival_flight, self.guest.invite_status),
                         ("Kojo Owusu", "BA81", "completed"))
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.traveller_changes[-1]["kind"], "guest_rename")

    def test_reinviting_another_address_revokes_the_old_link(self):
        _, old = self.invite()
        _, new = self.invite("someone@example.com")
        self.assertEqual(self.client.get(f"/api/bookings/invite/{old}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/bookings/invite/{new}/").status_code, 200)

    def test_only_the_owner_can_invite_and_not_the_lead_guest(self):
        stranger = APIClient()
        stranger.force_authenticate(get_user_model().objects.create_user(
            email="x@example.com", password="x-Strong-pass-1", first_name="X", last_name="Y"))
        url = f"/api/bookings/{self.booking.reference}/travellers/{self.guest.id}/invite/"
        self.assertEqual(stranger.post(url, {"email": "a@example.com"}, format="json").status_code, 404)
        lead_row = self.booking.travellers.get(is_lead=True)
        res = self.lead_api.post(f"/api/bookings/{self.booking.reference}/travellers/{lead_row.id}/invite/",
                                 {"email": "a@example.com"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_guest_dashboard_shows_only_their_own_part(self):
        _, token = self.invite()
        User = get_user_model()
        wrong = APIClient()
        wrong.force_authenticate(User.objects.create_user(email="x@example.com", password="x-Strong-pass-1", first_name="X", last_name="Y"))
        self.assertEqual(wrong.post(f"/api/bookings/invite/{token}/claim/").status_code, 403)

        kojo = User.objects.create_user(email="Kojo@Example.com", password="x-Strong-pass-1", first_name="Kojo", last_name="O")
        api = APIClient()
        api.force_authenticate(kojo)
        self.assertEqual(api.post(f"/api/bookings/invite/{token}/claim/").status_code, 403)  # email not verified yet
        User.objects.filter(pk=kojo.pk).update(email_verified=True)
        kojo.refresh_from_db()
        self.assertEqual(api.post(f"/api/bookings/invite/{token}/claim/").status_code, 200)
        trips = api.get("/api/bookings/trips/").json()
        self.assertEqual(len(trips), 1)
        self.assertEqual((trips[0]["reference"], trips[0]["traveller_id"]), (self.booking.reference, str(self.guest.id)))
        self.assertEqual([t["full_name"] for t in trips], ["G1"])  # never the lead guest's row
        self.assertEqual(api.get("/api/bookings/mine/").json(), [])  # the lead's booking stays the lead's
        self.assertEqual(self.lead_api.get("/api/bookings/trips/").json(), [])


class LinkedBookingTests(TestCase):
    setUpTestData = classmethod(base.ComponentPricingTests.setUpTestData.__func__)

    def setUp(self):
        from packages.models import RoomUnit, StayType
        RoomUnit.objects.create(stay_type=StayType.objects.get(package=self.pkg, code="airbnb_1bed"), label="spare")

    def book(self, email, linked_to=""):
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        return self.client.post("/api/bookings/checkout/component/", {
            "package_id": str(self.pkg.id), "mode": "couple", "hold_token": token,
            "travellers": [{"name": "A B"}, {"name": "C D"}],
            "rooms": [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}],
            "first_name": "Ama", "last_name": "Owusu", "email": email, "linked_to": linked_to,
        }, content_type="application/json")

    def test_second_airbnb_links_under_the_same_lead(self):
        first = self.book("ama@example.com").json()["reference"]
        self.assertEqual(self.book("other@example.com", first).status_code, 400)
        res = self.book("AMA@example.com", first)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["linked_to"], first)
        second = Booking.objects.get(reference=res.json()["reference"])
        self.assertEqual(second.linked_to.reference, first)
        detail = self.client.get(f"/api/bookings/{first}/", {"t": Booking.objects.get(reference=first).view_token}).json()
        self.assertEqual([b["reference"] for b in detail["linked_bookings"]], [second.reference])

    def test_linked_booking_gets_the_whole_groups_discount(self):
        """Couple + linked couple = a group of 4, so the second booking gets 5% off its total."""
        from packages.models import PackageDiscountRule
        PackageDiscountRule.objects.filter(package=self.pkg).update(is_active=True)
        self.pkg.price_lists.all().delete()
        first = self.book("ama@example.com")
        self.assertEqual(first.json()["total_amount"], "2898.00")  # 2 guests: no discount
        ref = first.json()["reference"]

        body = {"package_id": str(self.pkg.id), "mode": "couple", "travellers": [{}, {}],
                "rooms": [{"stay_type": "airbnb_1bed", "occupants": [0, 1]}], "linked_to": ref}
        preview = lambda: self.client.post("/api/bookings/quote/", body, content_type="application/json").json()
        self.assertEqual(preview()["discount_total"], "0.00")  # an unpaid booking doesn't count toward the group
        Booking.objects.filter(reference=ref).update(amount_paid=2898)
        self.assertEqual((preview()["discount_total"], preview()["total"]), ("144.90", "2753.10"))  # 5% of 2,898

        second = self.book("ama@example.com", ref)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["total_amount"], "2753.10")
