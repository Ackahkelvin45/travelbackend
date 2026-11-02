"""The back office: forms really save, access is enforced on the server, and
money and guests can't be changed around the services that keep them right."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone

from bookings import tests as pricing_tests
from packages.models import PriceListVersion, RoomUnit, StayType, TravelPackage
from payments.models import Payment, Refund
from payments.services import record_offline_payment
from reviews.models import Review

from .models import Booking, BookingTraveller, CancellationRequest, Lead

User = get_user_model()


class BackOfficeTests(TestCase):
    setUpTestData = pricing_tests.ComponentPricingTests.__dict__["setUpTestData"]
    checkout = pricing_tests.ComponentPricingTests.checkout

    def setUp(self):
        from .services import publish_price_list
        self.root = User.objects.create_superuser(email="root@test.com", password="x")
        publish_price_list(self.pkg, user=self.root, note="t")

    def login(self, *codenames):
        user = User.objects.create_user(email=f"staff{User.objects.count()}@test.com", password="x", is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.force_login(user)
        return user

    def booked(self, pay=None):
        from .services import create_hold
        token, _, _ = create_hold(self.pkg, ["airbnb_1bed"])
        booking = self.checkout(token)
        if pay:
            record_offline_payment(booking, amount=Decimal(pay), method=Payment.Method.BANK_TRANSFER, note="t")
            booking.refresh_from_db()
        return booking

    def form_data(self, url, **changes):
        """Everything the page would post, so a test saves the real form."""
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        data = {}
        forms = [page.context["adminform"].form]
        for inline in page.context["inline_admin_formsets"]:
            fs = inline.formset
            forms += [fs.management_form, *fs.forms]
        for form in forms:
            for bf in form:
                value = bf.value()
                if value is None or value is False:
                    continue
                data[bf.html_name] = "on" if value is True else [str(v) for v in value] if isinstance(value, (list, tuple)) else str(value)
        return {**data, **changes}

    def booking_url(self, b, suffix="change"):
        return f"/admin/bookings/booking/{b.pk}/{suffix}/"

    # ── Booking page ─────────────────────────────────────────────────────────
    def test_saving_a_booking_works_and_assigns_a_coordinator(self):
        self.client.force_login(self.root)
        b = self.booked(pay="2899.00")
        url = self.booking_url(b)
        res = self.client.post(url, self.form_data(url, coordinator=str(self.root.pk)))
        self.assertEqual(res.status_code, 302)
        b.refresh_from_db()
        self.assertEqual(b.coordinator, self.root)
        self.assertIsNotNone(b.coordinator_assigned_at)

    def test_status_and_money_cannot_be_posted_around_the_services(self):
        self.client.force_login(self.root)
        b = self.booked()
        url = self.booking_url(b)
        self.client.post(url, self.form_data(url, status="confirmed", num_guests="9", email="x@test.com"))
        b.refresh_from_db()
        self.assertEqual((b.status, b.num_guests, b.email), ("pending", 2, "ama@example.com"))

    def test_no_hand_made_bookings(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get("/admin/bookings/booking/add/").status_code, 403)

    def test_cancel_button_goes_through_the_service(self):
        self.client.force_login(self.root)
        b = self.booked()
        self.assertEqual(self.client.get(self.booking_url(b, "cancel-booking")).status_code, 302)
        b.refresh_from_db()
        self.assertEqual(b.status, "pending")  # a link alone does nothing
        self.client.post(self.booking_url(b, "cancel-booking"))
        b.refresh_from_db()
        self.assertEqual(b.status, "cancelled")
        self.assertIsNotNone(b.cancelled_at)
        self.assertFalse(b.room_units.exists())

    def test_change_log_shows_on_the_booking(self):
        self.client.force_login(self.root)
        b = self.booked()
        Booking.objects.filter(pk=b.pk).update(traveller_changes=[{"kind": "name", "from": "Al", "to": "Ali", "at": "2026-11-01T10:00", "by": "s@x"}])
        self.assertContains(self.client.get(self.booking_url(b)), "to: Ali")

    # ── Access on the server ─────────────────────────────────────────────────
    def test_staff_without_permission_are_refused_every_button(self):
        b = self.booked(pay="2899.00")
        lead = Lead.objects.create(name="A", email="a@test.com")
        ticket = b.travellers.first()
        self.login()
        for url in (f"/admin/packages/travelpackage/{self.pkg.pk}/publish-prices/", self.booking_url(b, "send-reminder"),
                    self.booking_url(b, "resend-confirmation"), self.booking_url(b, "mark-contacted"),
                    self.booking_url(b, "cancel-booking"), self.booking_url(b, "create-refund"),
                    f"/admin/bookings/lead/{lead.pk}/mark-replied/", f"/admin/bookings/eventticket/{ticket.pk}/check-in/"):
            with self.subTest(url):
                self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(PriceListVersion.objects.filter(package=self.pkg).count(), 1)  # only setUp's

    def test_view_only_staff_are_offered_no_state_changing_bulk_action(self):
        self.login(*Permission.objects.filter(codename__startswith="view_").values_list("codename", flat=True))
        for url in ("bookings/booking", "bookings/lead", "bookings/chatthread", "bookings/eventticket", "bookings/policydocument",
                    "packages/travelpackage", "packages/roomunit", "packages/staytype", "packages/tripupdate",
                    "payments/payment", "payments/refund", "payments/scheduledtask", "blog/blog", "reviews/review",
                    "newsletter/newslettersubscriber", "accounts/user"):
            with self.subTest(url):
                page = self.client.get(f"/admin/{url}/")
                self.assertEqual(page.status_code, 200)
                form = page.context.get("action_form")
                names = {c for c, _ in form.fields["action"].choices} - {""} if form else set()
                self.assertEqual(names - {"export_csv"}, set(), url)

    # ── Accounts ─────────────────────────────────────────────────────────────
    def test_managing_accounts_cannot_raise_anyones_access(self):
        from django.core.management import call_command
        call_command("setup_staff_groups", verbosity=0)
        finance = Group.objects.get(name="Azura Finance")
        target = User.objects.create_user(email="t@test.com", password="old-pass", is_staff=True)
        target.groups.add(finance)
        me = self.login("view_user", "change_user", "add_user")
        self.assertEqual(self.client.post(f"/admin/accounts/user/{target.pk}/password/", {
            "password1": "Taken-Over-1", "password2": "Taken-Over-1"}).status_code, 403)
        url = f"/admin/accounts/user/{me.pk}/change/"
        page = self.client.get(url)
        form = page.context["adminform"].form
        self.assertNotIn(finance, form.fields["groups"].queryset)  # can't join a stronger group
        self.client.post(url, {"email": me.email, "groups": [finance.pk], "is_active": "on", "is_staff": "on"})
        self.assertFalse(User.objects.get(pk=me.pk).groups.exists())

    def test_only_superusers_change_groups(self):
        self.login("view_group", "change_group", "delete_group", "add_group")
        group = Group.objects.create(name="G")
        self.assertEqual(self.client.post(f"/admin/auth/group/{group.pk}/change/", {"name": "H"}).status_code, 403)

    def test_inactive_strong_accounts_cannot_be_reset_by_weaker_staff(self):
        target = User.objects.create_user(email="t@test.com", password="p", is_staff=True, is_active=False)
        target.user_permissions.add(Permission.objects.get(codename="process_refund"))
        self.login("view_user", "change_user")
        self.assertEqual(self.client.post(f"/admin/accounts/user/{target.pk}/password/", {
            "password1": "Taken-Over-1", "password2": "Taken-Over-1"}).status_code, 403)

    # ── Stays and rooms ──────────────────────────────────────────────────────
    def test_stays_tab_offers_no_delete(self):
        self.client.force_login(self.root)
        page = self.client.get(f"/admin/packages/travelpackage/{self.pkg.pk}/change/")
        stays = next(i for i in page.context["inline_admin_formsets"] if i.formset.model is StayType)
        self.assertFalse(stays.formset.can_delete)

    def _dated(self, label_count=2):
        from .services import create_hold
        st = self.pkg.stay_types.get(code="airbnb_1bed_stay")
        for i in range(1, label_count):
            RoomUnit.objects.create(stay_type=st, label=f"extra-{i}")
        token, _, units = create_hold(self.pkg, [st.code], check_in="2027-01-20", check_out="2027-01-22")
        return st, units[0]

    def test_a_dated_stay_is_not_counted_as_a_unit_and_can_move(self):
        from .services import reassign_unit
        st, row = self._dated()
        self.client.force_login(self.root)
        page = self.client.get("/admin/packages/roomunit/")
        self.assertEqual(next(r for r in page.context["room_rows"] if r["stay"] == st)["total"], 2)
        booking = self.booked()
        RoomUnit.objects.filter(pk=row.pk).update(booking=booking, held_until=None)
        row.refresh_from_db()
        target = st.units.filter(check_in__isnull=True).exclude(label=row.label).first()
        reassign_unit(row, target)
        row.refresh_from_db()
        self.assertEqual(row.label, target.label)

    # ── Money ────────────────────────────────────────────────────────────────
    def test_refund_status_cannot_be_typed_and_the_button_updates_the_booking(self):
        from payments.refunds import create_fixed_refund
        b = self.booked(pay="2899.00")
        refund = create_fixed_refund(b, Decimal("100.00"), reason="r")[0]
        self.client.force_login(self.root)
        url = f"/admin/payments/refund/{refund.pk}/change/"
        self.client.post(url, self.form_data(url, status="processed"))
        refund.refresh_from_db()
        self.assertEqual(refund.status, "pending")
        self.client.post(f"/admin/payments/refund/{refund.pk}/mark-processed/")
        refund.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((refund.status, refund.processed_by, b.amount_refunded), ("processed", self.root, Decimal("100.00")))

    def test_offline_payment_is_limited_to_the_balance(self):
        self.client.force_login(self.root)
        b = self.booked()
        url = self.booking_url(b)
        data = self.form_data(url)
        prefix = "payments"
        data.update({f"{prefix}-TOTAL_FORMS": "1", f"{prefix}-0-amount": "99999.00", f"{prefix}-0-purpose": "installment",
                     f"{prefix}-0-method": "bank_transfer", f"{prefix}-0-booking": str(b.pk), f"{prefix}-0-note": "typo"})
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 200)  # the form came back with an error
        self.assertContains(res, "more than the")
        self.assertFalse(b.payments.exists())

    def test_create_refund_is_capped_and_needs_a_reason(self):
        b = self.booked(pay="1000.00")
        self.client.force_login(self.root)
        url = self.booking_url(b, "create-refund")
        self.client.post(url, {"amount": "5000", "reason": "x", "version": "0"})
        self.client.post(url, {"amount": "10", "reason": "", "version": "0"})
        self.assertFalse(Refund.objects.exists())
        self.client.post(url, {"amount": "900", "reason": "Goodwill", "version": "0"})
        self.client.post(url, {"amount": "200", "reason": "Again", "version": "1"})  # only 100 is left once 900 is queued
        self.assertEqual([r.amount for r in Refund.objects.all()], [Decimal("900.00")])

    # ── Event door ───────────────────────────────────────────────────────────
    def test_check_in_and_forfeit_can_be_undone(self):
        b = self.booked(pay="2899.00")
        self.client.force_login(self.root)
        t = b.travellers.first()
        BookingTraveller.objects.filter(pk=t.pk).update(checked_in_at=timezone.now(), checked_in_by=self.root)
        self.client.post(f"/admin/bookings/eventticket/{t.pk}/undo-check-in/")
        t.refresh_from_db()
        self.assertIsNone(t.checked_in_at)
        BookingTraveller.objects.filter(pk=t.pk).update(forfeited_at=timezone.now(), forfeited_by=self.root)
        self.client.post(f"/admin/bookings/eventticket/{t.pk}/undo-forfeit/")
        t.refresh_from_db()
        self.assertIsNone(t.forfeited_at)

    def test_an_access_exception_needs_a_note_and_records_who(self):
        b = self.booked(pay="1000.00")
        self.client.force_login(self.root)
        t = b.travellers.first()
        Booking.objects.filter(pk=b.pk).update(status=Booking.Status.CONFIRMED)
        url = f"/admin/bookings/eventticket/{t.pk}/change/"
        self.assertContains(self.client.post(url, {"access_exception": "on", "access_note": ""}), "who approved")
        self.client.post(url, {"access_exception": "on", "access_note": "Cash on arrival"})
        t.refresh_from_db()
        self.assertIn(self.root.email, t.access_note)

    def test_bulk_rules(self):
        self.client.force_login(self.root)
        cancelled, future = self.booked(), self.booked(pay="2899.00")
        Booking.objects.filter(pk=cancelled.pk).update(status="cancelled")
        act = lambda name, *objs: self.client.post("/admin/bookings/booking/", {
            "action": name, "index": 0, "_selected_action": [str(o.pk) for o in objs]}, follow=True)
        act("mark_arrived", cancelled)
        cancelled.refresh_from_db()
        self.assertIsNone(cancelled.arrived_at)
        self.assertContains(act("mark_completed", future), "1 skipped")
        future.refresh_from_db()
        self.assertEqual(future.status, "confirmed")

    def test_cancellation_requests_cannot_be_deleted_and_can_be_decided_on_their_page(self):
        b = self.booked(pay="2899.00")
        req = CancellationRequest.objects.create(booking=b)
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(f"/admin/bookings/cancellationrequest/{req.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.client.post(f"/admin/bookings/cancellationrequest/{req.pk}/reject/")
        req.refresh_from_db()
        self.assertEqual(req.status, "rejected")

    # ── Reviews, leads, exports ──────────────────────────────────────────────
    def test_hidden_reviews_leave_public_numbers_and_rating_sorts_work(self):
        Review.objects.create(package=self.pkg, reviewer_name="A", reviewer_email="a@t.com", rating=1, body="x", is_visible=False)
        TravelPackage.objects.filter(pk=self.pkg.pk).update(is_active=True)
        res = self.client.get(f"/api/packages/{self.pkg.pk}/").json()
        self.assertEqual((res["avg_rating"], res["review_count"]), (None, 0))
        for sort in ("top_rated", "most_reviewed"):
            self.assertEqual(self.client.get(f"/api/packages/?sort={sort}").status_code, 200)

    def test_marking_replied_under_the_waiting_filter_also_moves_the_stage(self):
        lead = Lead.objects.create(name="A", email="a@test.com")
        self.client.force_login(self.root)
        self.client.post("/admin/bookings/lead/?reply=waiting", {"action": "mark_replied", "index": 0, "_selected_action": [str(lead.pk)]})
        lead.refresh_from_db()
        self.assertEqual(lead.stage, Lead.Stage.CONTACTED)

    def test_phone_only_lead_reminder_says_so(self):
        lead = Lead.objects.create(name="A", phone="+233244123456", marketing_consent=True, quote={"total": "1"})
        self.client.force_login(self.root)
        res = self.client.post(f"/admin/bookings/lead/{lead.pk}/send-reminder/", follow=True)
        self.assertContains(res, "no email address")

    def test_exports_neutralise_spreadsheet_formulas(self):
        from django.http import HttpResponse
        from config.admin_tools import safe_csv
        response = HttpResponse()
        safe_csv(response).writerow(["=HYPERLINK(1)", "+1", "plain", 5])
        self.assertTrue(response.content.startswith(b"'=HYPERLINK(1),'+1,plain,5"))


class BackOfficeRoundTwoTests(BackOfficeTests):
    """What the second review found: money that vanished or came back as debt,
    and booking changes staff couldn't make."""

    def rows(self, url, **fields):
        data = self.form_data(url)
        data.update({"payments-TOTAL_FORMS": "1", **{f"payments-0-{k}": v for k, v in fields.items()}})
        return data

    def test_an_offline_payment_row_is_actually_recorded(self):
        self.client.force_login(self.root)
        b = self.booked()
        url = self.booking_url(b)
        res = self.client.post(url, self.rows(url, amount="500.00", purpose="installment", method="bank_transfer",
                                              booking=str(b.pk), note="Bank"))
        self.assertEqual(res.status_code, 302)
        payment = b.payments.get()
        b.refresh_from_db()
        self.assertEqual((payment.status, payment.amount, b.amount_paid), ("success", Decimal("500.00"), Decimal("500.00")))
        self.assertIn(self.root.email, payment.note)

    def test_money_reaching_a_cancelled_booking_can_be_recorded_and_refunded(self):
        self.client.force_login(self.root)
        b = self.booked()
        Booking.objects.filter(pk=b.pk).update(status="cancelled")
        url = self.booking_url(b)
        self.client.post(url, self.rows(url, amount="500.00", purpose="installment", method="bank_transfer",
                                        booking=str(b.pk), note="Late"))
        self.assertTrue(b.payments.get().needs_review)
        self.client.post(self.booking_url(b, "create-refund"), {"amount": "500", "reason": "Reached a closed booking", "version": "0"})
        self.assertEqual(Refund.objects.get().amount, Decimal("500.00"))

    def test_staff_rest_api_writes_need_a_superuser(self):
        from rest_framework.test import APIClient
        staff = User.objects.create_user(email="door@test.com", password="x", is_staff=True)
        api = APIClient()
        api.force_authenticate(staff)
        url = f"/api/packages/{self.pkg.pk}/"
        self.assertEqual(api.patch(url, {"title": "Hacked"}, format="json").status_code, 403)
        self.assertEqual(api.delete(url).status_code, 403)
        self.assertTrue(TravelPackage.objects.filter(pk=self.pkg.pk).exclude(title="Hacked").exists())

    def _process(self):
        from payments.refunds import mark_refund_processed
        for refund in Refund.objects.filter(status="pending"):
            mark_refund_processed(refund, by_user=self.root)

    def test_goodwill_refund_lowers_the_price_so_nothing_is_owed(self):
        self.client.force_login(self.root)
        b = self.booked(pay="2898.00")
        self.assertTrue(b.is_paid)
        self.client.post(self.booking_url(b, "create-refund"), {"amount": "100", "reason": "Goodwill", "version": "0"})
        self._process()
        b.refresh_from_db()
        self.assertEqual((b.total_amount, b.balance, b.is_paid), (Decimal("2798.00"), Decimal("0.00"), True))
        self.assertTrue(any(l["code"] == "price_adjustment" for l in b.addons))
        from payments.receipts import compute_line_items
        self.assertTrue(compute_line_items(b)["reconciles"])

    def test_overpayment_refund_leaves_the_price_alone(self):
        self.client.force_login(self.root)
        b = self.booked(pay="3000.00")
        self.client.post(self.booking_url(b, "create-refund"), {"amount": "102", "reason": "Paid too much", "version": "0"})
        self._process()
        b.refresh_from_db()
        self.assertEqual((b.total_amount, b.balance), (Decimal("2898.00"), Decimal("0.00")))

    def test_approving_with_a_refund_queued_explains_instead_of_crashing(self):
        from payments.refunds import create_fixed_refund
        b = self.booked(pay="2898.00")
        create_fixed_refund(b, Decimal("10.00"), reason="r")
        req = CancellationRequest.objects.create(booking=b)
        self.client.force_login(self.root)
        res = self.client.post(f"/admin/bookings/cancellationrequest/{req.pk}/approve/", follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "pending refunds")

    def test_staff_cancelling_closes_the_guests_request(self):
        from .services import cancel_booking
        b = self.booked()
        req = CancellationRequest.objects.create(booking=b)
        cancel_booking(b, reason=Booking.CancellationReason.ADMIN)
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")

    def test_account_managers_cannot_take_over_a_customer(self):
        customer = User.objects.create_user(email="guest@test.com", password="old-pass")
        self.login("view_user", "change_user")
        self.assertEqual(self.client.post(f"/admin/accounts/user/{customer.pk}/password/", {
            "password1": "Taken-Over-1", "password2": "Taken-Over-1"}).status_code, 403)
        self.client.post(f"/admin/accounts/user/{customer.pk}/change/", {
            "email": "mine@test.com", "is_active": "on"})
        customer.refresh_from_db()
        self.assertEqual(customer.email, "guest@test.com")

    def test_a_booking_with_forfeited_tickets_can_be_closed(self):
        self.client.force_login(self.root)
        b = self.booked(pay="1000.00")
        Booking.objects.filter(pk=b.pk).update(status="confirmed", travel_date="2026-01-06")
        b.travellers.update(forfeited_at=timezone.now())
        self.client.post("/admin/bookings/booking/", {"action": "mark_completed", "index": 0, "_selected_action": [str(b.pk)]})
        b.refresh_from_db()
        self.assertEqual(b.status, "completed")

    def test_a_unit_with_a_live_dated_stay_cannot_be_deleted(self):
        st, row = self._dated()
        booking = self.booked()
        RoomUnit.objects.filter(pk=row.pk).update(booking=booking, held_until=None)
        base = st.units.get(check_in__isnull=True, label=row.label)
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(f"/admin/packages/roomunit/{base.pk}/delete/", {"post": "yes"}).status_code, 403)

    def test_bad_input_on_refund_and_addon_pages_is_not_a_crash(self):
        self.client.force_login(self.root)
        b = self.booked(pay="1000.00")
        self.assertEqual(self.client.post(self.booking_url(b, "create-refund"), {"amount": "NaN", "reason": "x", "version": "0"}).status_code, 200)
        self.assertEqual(self.client.post(self.booking_url(b, "remove-addon"), {"code": "not-a-uuid:enzo_vip"}).status_code, 200)

    # ── Change room type ─────────────────────────────────────────────────────
    def test_changing_room_type_moves_guests_and_reprices(self):
        from .services import change_room_type, publish_price_list
        from payments.receipts import compute_line_items
        publish_price_list(self.pkg, user=self.root, note="t")
        b = self.booked(pay="2898.00")
        Booking.objects.filter(pk=b.pk).update(status="confirmed")
        b.refresh_from_db()
        unit = b.room_units.get()
        delta = change_room_type(b, unit, "airbnb_2bed", by_email="s@x")
        self.assertEqual(delta, Decimal("600.00"))  # 5 × (340 − 220)
        b.refresh_from_db()
        new = b.room_units.get()
        self.assertEqual(new.stay_type.code, "airbnb_2bed")
        self.assertEqual({t.unit_id for t in b.travellers.all()}, {new.pk})
        self.assertEqual((b.total_amount, b.balance), (Decimal("3498.00"), Decimal("600.00")))
        self.assertEqual(sum(t.share for t in b.travellers.all()), b.total_amount)
        self.assertTrue(compute_line_items(b)["reconciles"])
        unit.refresh_from_db()
        self.assertIsNone(unit.booking)

    def test_a_cheaper_room_refunds_the_difference_and_full_rooms_are_refused(self):
        from .services import IllegalTransition, change_room_type, create_hold, publish_price_list
        publish_price_list(self.pkg, user=self.root, note="t")
        token, _, _ = create_hold(self.pkg, ["airbnb_2bed"])
        b = self.checkout(token, rooms=[{"stay_type": "airbnb_2bed", "occupants": [0, 1]}])
        record_offline_payment(b, amount=b.total_amount, method=Payment.Method.BANK_TRANSFER, note="t")
        b.refresh_from_db()
        delta = change_room_type(b, b.room_units.get(), "airbnb_1bed", by_email="s@x")
        self.assertEqual(delta, Decimal("-600.00"))
        self.assertEqual(Refund.objects.get().amount, Decimal("600.00"))
        with self.assertRaises(IllegalTransition):
            change_room_type(b, b.room_units.get(), "fourpoints_standard", by_email="s@x")  # sleeps one

    # ── Remove a guest ───────────────────────────────────────────────────────
    def test_removing_a_guest_keeps_a_shared_rooms_price_with_the_roommate(self):
        from .services import IllegalTransition, remove_guest
        from payments.receipts import compute_line_items
        b = self.booked(pay="2898.00")
        Booking.objects.filter(pk=b.pk).update(status="confirmed")
        b.refresh_from_db()
        lead, guest = b.travellers.order_by("position")
        with self.assertRaises(IllegalTransition):
            remove_guest(lead, by_email="s@x")
        off = remove_guest(guest, by_email="s@x", note="Can't travel")
        self.assertEqual(off, Decimal("899.00"))  # Core only: the room stays, charged once
        b.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual((b.num_guests, b.mode, b.total_amount), (1, "single", Decimal("1999.00")))
        self.assertEqual(lead.share, b.total_amount)
        self.assertEqual(Refund.objects.get().amount, Decimal("899.00"))
        self.assertTrue(compute_line_items(b)["reconciles"])
        self.assertEqual(lead.ticket_code, f"{b.reference}-T1")

    # ── Repeated changes, priced by the engine ───────────────────────────────
    def trip(self, stay, n, pay=None, plan="full", experiences=None, percent=None, separately=False):
        from .services import create_component_booking, create_hold
        if percent:
            from .services import publish_price_list
            self.pkg.discount_rules.update(is_active=True)
            publish_price_list(self.pkg, user=self.root, note="discount")
        token, _, _ = create_hold(self.pkg, [stay])
        mode = "couple" if n == 2 else "group" if n > 2 else "single"
        b = create_component_booking(
            package=self.pkg, mode=mode, rooms=[{"stay_type": stay, "occupants": list(range(n))}], payment_plan=plan,
            travellers=[{"name": f"G{i}", "experiences": (experiences or {}).get(i, []), "adult": True} for i in range(n)],
            hold_token=token, accepted_policy_types=[], pay_separately=separately,
            contact={"first_name": "Ama", "last_name": "Owusu", "email": "ama@example.com"},
        )
        if pay:
            record_offline_payment(b, amount=Decimal(pay), method=Payment.Method.BANK_TRANSFER, note="t")
        Booking.objects.filter(pk=b.pk).update(status="confirmed")
        b.refresh_from_db()
        return b

    def assertConsistent(self, b):
        from payments.receipts import compute_line_items
        b.refresh_from_db()
        items = compute_line_items(b)
        self.assertTrue(items["reconciles"], items)
        self.assertEqual(sum(t.share for t in b.travellers.all()), b.total_amount)
        self.assertEqual(b.num_guests, b.travellers.count())

    def test_removing_two_from_a_shared_room_keeps_one_full_room_charge(self):
        from .services import remove_guest
        b = self.trip("airbnb_2bed", 3)
        for guest in list(b.travellers.filter(is_lead=False)):
            remove_guest(guest, by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, Decimal("2599.00"))  # 899 + 5 × 340

    def test_room_change_then_removal(self):
        from .services import change_room_type, remove_guest
        b = self.trip("airbnb_1bed", 2)
        change_room_type(b, b.room_units.get(), "airbnb_2bed", by_email="s@x")
        b.refresh_from_db()
        remove_guest(b.travellers.get(is_lead=False), by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, Decimal("2599.00"))

    def test_the_booked_discount_survives_guests_dropping_out(self):
        from .services import remove_guest
        b = self.trip("airbnb_2bed", 4, percent=5)
        self.assertEqual(b.total_amount, Decimal("5031.20"))  # (4 × 899 + 1700) × 0.95
        for guest in list(b.travellers.filter(is_lead=False))[:2]:
            remove_guest(guest, by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, Decimal("3323.10"))  # (2 × 899 + 1700) × 0.95

    def test_transfer_riders_keep_paying_for_the_vehicle(self):
        from .services import remove_guest
        pickup = ["airport_pickup"]
        b = self.trip("airbnb_2bed", 3, experiences={0: pickup, 1: pickup, 2: pickup})
        before = b.total_amount
        remove_guest(b.travellers.order_by("position").last(), by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, before - Decimal("899.00") - Decimal("40.00"))  # 3 riders needed 2 vehicles, 2 need 1
        remove_guest(b.travellers.order_by("position").last(), by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, Decimal("2639.00"))
        self.assertTrue(any(l["code"] == "airport_pickup" for l in b.addons))

    def test_goodwill_then_cancelling_refunds_no_more_than_was_paid(self):
        from .services import lower_total
        from payments.refunds import compute_refund
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        lower_total(b, Decimal("500.00"), label="Goodwill", by_email="s@x")
        b.refresh_from_db()
        self.assertLessEqual(compute_refund(b, full=True)["refund_total"], Decimal("2898.00"))

    def test_a_cut_bigger_than_one_share_is_spread(self):
        from .services import lower_total
        b = self.trip("airbnb_1bed", 2)
        lower_total(b, Decimal("2000.00"), label="Goodwill", by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.total_amount, Decimal("898.00"))

    def test_create_refund_submitted_twice_counts_once(self):
        self.client.force_login(self.root)
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        version = str(b.refunds.count())
        for _ in range(2):
            self.client.post(self.booking_url(b, "create-refund"), {"amount": "100", "reason": "Goodwill", "version": version})
        self.assertEqual(b.refunds.count(), 1)
        b.refresh_from_db()
        self.assertEqual(b.total_amount, Decimal("2798.00"))

    def test_no_changes_while_a_refund_is_waiting(self):
        from .services import IllegalTransition, change_room_type
        b = self.trip("airbnb_2bed", 2, pay="3498.00")
        change_room_type(b, b.room_units.get(), "airbnb_1bed", by_email="s@x")
        b.refresh_from_db()
        with self.assertRaisesRegex(IllegalTransition, "refund waiting"):
            change_room_type(b, b.room_units.get(), "airbnb_2bed", by_email="s@x")

    def test_deposits_follow_changes(self):
        from .services import change_room_type, remove_guest
        b = self.trip("airbnb_1bed", 2, plan="installment")
        self.assertEqual(b.deposit_required, Decimal("2348.00"))  # Core in full + 50% of 1100
        change_room_type(b, b.room_units.get(), "airbnb_2bed", by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.deposit_required, Decimal("2648.00"))
        remove_guest(b.travellers.get(is_lead=False), by_email="s@x")
        self.assertConsistent(b)
        self.assertEqual(b.deposit_required, Decimal("1749.00"))  # 899 + 850

    def test_a_guest_who_paid_for_themselves_gets_their_own_payment_back(self):
        from .services import remove_guest
        b = self.trip("airbnb_2bed", 3, pay="3498.00", separately=True)
        guest = b.travellers.order_by("position").last()
        own = record_offline_payment(b, amount=Decimal("899.00"), method=Payment.Method.BANK_TRANSFER, note="own").payment
        Payment.objects.filter(pk=own.pk).update(traveller=guest)
        b.refresh_from_db()
        remove_guest(guest, by_email="s@x")
        leg = b.refunds.get()
        self.assertEqual((leg.payment_id, leg.amount), (own.pk, Decimal("899.00")))
        own.refresh_from_db()
        self.assertIn("since removed", own.note)

    def test_refund_policy_can_be_applied_when_removing_a_guest(self):
        from .services import remove_guest
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        remove_guest(b.travellers.get(is_lead=False), by_email="s@x", refund=Decimal("539.40"))  # 60% of 899
        self.assertConsistent(b)
        self.assertEqual(b.refunds.get().amount, Decimal("539.40"))
        self.assertEqual(b.total_amount, Decimal("2358.60"))  # 1999 + the 359.60 kept

    def test_changes_wait_for_an_open_chargeback(self):
        from .services import IllegalTransition, remove_guest
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        b.payments.update(disputed=True)
        with self.assertRaisesRegex(IllegalTransition, "chargeback"):
            remove_guest(b.travellers.get(is_lead=False), by_email="s@x")

    def _two_rooms(self):
        from .services import create_component_booking, create_hold
        token, _, _ = create_hold(self.pkg, ["fourpoints_standard", "fourpoints_standard"])
        b = create_component_booking(
            package=self.pkg, mode="couple", payment_plan="full", hold_token=token, accepted_policy_types=[],
            rooms=[{"stay_type": "fourpoints_standard", "occupants": [0]}, {"stay_type": "fourpoints_standard", "occupants": [1]}],
            travellers=[{"name": "A"}, {"name": "B"}], contact={"first_name": "A", "last_name": "B", "email": "a@test.com"},
        )
        Booking.objects.filter(pk=b.pk).update(status="confirmed")
        b.refresh_from_db()
        return b

    def test_an_empty_room_must_be_released_not_silently_dropped(self):
        from .services import IllegalTransition, change_room_type, release_empty_room
        b = self._two_rooms()
        record_offline_payment(b, amount=b.total_amount, method=Payment.Method.BANK_TRANSFER, note="t")
        b.refresh_from_db()
        first, second = b.travellers.order_by("position")
        # One guest moved to their own stay on the Guests tab, leaving a paid room empty.
        empty = second.unit
        BookingTraveller.objects.filter(pk=second.pk).update(unit=None)
        with self.assertRaisesRegex(IllegalTransition, "no guests"):
            change_room_type(b, first.unit, "fourpoints_couple", by_email="s@x")
        self.assertEqual(release_empty_room(b, empty, by_email="s@x"), Decimal("-1320.00"))
        self.assertConsistent(b)
        self.assertEqual(b.refunds.get().amount, Decimal("1320.00"))
        empty.refresh_from_db()
        self.assertIsNone(empty.booking)

    def test_add_ons_bought_later_keep_the_price_paid(self):
        from .services import change_room_type
        b = self.trip("airbnb_2bed", 2, pay="3498.00")
        guest = b.travellers.order_by("position").last()
        BookingTraveller.objects.filter(pk=guest.pk).update(experiences=["enzo_vip"], share=guest.share + 300)
        Booking.objects.filter(pk=b.pk).update(total_amount=b.total_amount + 300)
        record_offline_payment(b, amount=Decimal("300.00"), method=Payment.Method.BANK_TRANSFER, note="addon",
                               purpose=Payment.Purpose.ADDON)
        Payment.objects.filter(purpose="addon").update(addon_code="enzo_vip", addon_travellers=[str(guest.pk)])
        b.refresh_from_db()
        b.addons = [*b.addons, {"code": "enzo_vip", "name": "ENZO", "unit": "per_person", "unit_price": "300", "quantity": 1,
                                "line_total": "300.00", "refundable": True}]
        b.save(update_fields=["addons"])
        delta = change_room_type(b, b.room_units.get(), "airbnb_1bed", by_email="s@x")
        self.assertEqual(delta, Decimal("-600.00"))  # the fixture's $220 list price doesn't touch the $300 paid
        self.assertConsistent(b)

    def test_a_guest_who_paid_is_refunded_even_if_someone_else_owes(self):
        from .services import remove_guest, ticket_outstanding
        b = self.trip("airbnb_2bed", 3, separately=True)
        guest = b.travellers.order_by("position").last()
        paid = guest.share
        own = record_offline_payment(b, amount=paid, method=Payment.Method.BANK_TRANSFER, note="own").payment
        Payment.objects.filter(pk=own.pk).update(traveller=guest)
        b.refresh_from_db()
        remove_guest(guest, by_email="s@x", refund=paid)
        self.assertEqual(b.refunds.get().amount, paid)
        self.assertGreater(ticket_outstanding(b.travellers.order_by("position").last()), 0)

    def test_a_new_room_uses_the_booked_price_when_the_list_already_has_it(self):
        from .pricing import live_catalog
        from .services import _requote, publish_price_list
        b = self._two_rooms()
        StayType.objects.filter(package=self.pkg, code="fourpoints_standard").update(price_per_night=Decimal("400"))
        publish_price_list(self.pkg, user=self.root, note="dearer")
        live = live_catalog(self.pkg).stay_types["fourpoints_standard"]
        guests = list(b.travellers.order_by("position"))
        rooms = [{"stay_type": "fourpoints_standard", "occupants": [0]}, {"stay_type": "fourpoints_standard", "occupants": [1]}]
        self.assertEqual(_requote(b, guests, rooms, "couple", extra_stay=live).total, b.total_amount)

    def test_a_booking_with_no_price_list_is_refused_not_guessed(self):
        from .services import IllegalTransition, remove_guest
        b = self.trip("airbnb_1bed", 2)
        Booking.objects.filter(pk=b.pk).update(price_list_version=None)
        b.refresh_from_db()
        with self.assertRaisesRegex(IllegalTransition, "price list"):
            remove_guest(b.travellers.get(is_lead=False), by_email="s@x")

    def test_guest_dues_add_up_to_the_booking_after_a_removed_guests_refund(self):
        from .services import remove_guest, ticket_outstanding
        b = self.trip("airbnb_2bed", 3, separately=True)
        guest, other = b.travellers.order_by("-position")[:2]
        paid = guest.share
        own = record_offline_payment(b, amount=paid, method=Payment.Method.BANK_TRANSFER, note="own").payment
        Payment.objects.filter(pk=own.pk).update(traveller=guest)
        b.refresh_from_db()
        remove_guest(guest, by_email="s@x", refund=paid)
        self._process()
        b.refresh_from_db()
        self.assertEqual(sum(row[3] for row in b.traveller_balances()), b.balance)
        other.refresh_from_db()
        self.assertEqual(ticket_outstanding(other), other.share)

    def test_a_refunded_add_on_payment_keeps_its_price(self):
        from .services import _bought_later
        b = self.trip("airbnb_2bed", 2, pay="3498.00")
        guest = b.travellers.order_by("position").last()
        BookingTraveller.objects.filter(pk=guest.pk).update(experiences=["enzo_vip"])
        guest.refresh_from_db()
        Payment.objects.create(booking=b, amount=Decimal("300.00"), currency="USD", purpose="addon", status="refunded",
                               addon_code="enzo_vip", addon_travellers=[str(guest.pk)])
        self.assertEqual(_bought_later(b, [guest]), {guest.pk: {"enzo_vip": Decimal("300.00")}})

    def test_releasing_takes_every_empty_room_together(self):
        from .services import create_component_booking, create_hold, release_empty_room
        RoomUnit.objects.create(stay_type=self.pkg.stay_types.get(code="fourpoints_standard"), label="extra")
        token, _, _ = create_hold(self.pkg, ["fourpoints_standard"] * 3)
        b = create_component_booking(
            package=self.pkg, mode="group", payment_plan="full", hold_token=token, accepted_policy_types=[],
            rooms=[{"stay_type": "fourpoints_standard", "occupants": [i]} for i in range(3)],
            travellers=[{"name": n} for n in "ABC"], contact={"first_name": "A", "last_name": "B", "email": "a@test.com"},
        )
        Booking.objects.filter(pk=b.pk).update(status="confirmed")
        b.refresh_from_db()
        first, second, third = b.travellers.order_by("position")
        empty = second.unit
        b.travellers.exclude(pk=first.pk).update(unit=None)
        self.assertEqual(release_empty_room(b, empty, by_email="s@x"), Decimal("-2640.00"))
        self.assertConsistent(b)
        self.assertEqual(b.room_units.count(), 1)

    def test_removal_page_shows_what_can_go_back(self):
        self.client.force_login(self.root)
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        self.assertContains(self.client.get(self.booking_url(b, "remove-guest")), "899.00 can go back to the guest")

    def test_a_missed_chargeback_webhook_can_be_cleared_by_finance(self):
        b = self.trip("airbnb_1bed", 2, pay="2898.00")
        b.payments.update(disputed=True)
        self.client.force_login(self.root)
        self.client.post("/admin/payments/payment/", {"action": "clear_chargeback", "index": 0,
                                                      "_selected_action": [str(b.payments.get().pk)]})
        self.assertFalse(b.payments.get().disputed)

    def test_consent_can_be_recorded_off_but_not_on(self):
        self.client.force_login(self.root)
        b = self.booked()
        url = self.booking_url(b)
        self.assertContains(self.client.post(url, self.form_data(url, marketing_consent="on")), "Only the guest can opt in")

    def test_room_and_guest_pages_work(self):
        self.client.force_login(self.root)
        b = self.booked(pay="2898.00")
        for path in ("change-room", "remove-guest"):
            self.assertEqual(self.client.get(self.booking_url(b, path)).status_code, 200)
        guest = b.travellers.get(is_lead=False)
        res = self.client.post(self.booking_url(b, "remove-guest"), {"guest": str(guest.pk), "refund": "0"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(b.travellers.count(), 1)
