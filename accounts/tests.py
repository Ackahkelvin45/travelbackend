from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.emails import make_verification_token, read_verification_token
from accounts.models import User


@override_settings(FRONTEND_URL="https://azuratravels.live", RESEND_API_KEY="re_test_key")
class SignupVerificationTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # login/signup throttles count per IP across tests

    def _register(self):
        return APIClient().post("/api/auth/register/", {
            "email": "newuser@test.com", "first_name": "New", "last_name": "User",
            "password": "Signup#Pass123", "password_confirm": "Signup#Pass123",
        }, format="json")

    @patch("accounts.views.send_verification_email")
    def test_signup_creates_unverified_user_and_sends_verification(self, mock_verify):
        r = self._register()
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.data["email_verified"])
        user = User.objects.get(email="newuser@test.com")
        self.assertFalse(user.email_verified)
        self.assertEqual(mock_verify.call_count, 1)
        # the link passed to the mail carries a valid token for this user
        sent_url = mock_verify.call_args[0][1]
        self.assertIn("token=", sent_url)

    @patch("accounts.views.send_welcome_email")
    @patch("accounts.views.send_verification_email")
    def test_verify_marks_verified_and_sends_welcome_once(self, _v, mock_welcome):
        self._register()
        user = User.objects.get(email="newuser@test.com")
        token = make_verification_token(user)

        r1 = APIClient().get(f"/api/auth/verify-email/?token={token}")
        self.assertEqual(r1.status_code, 302)
        self.assertIn("verified=1", r1.url)
        user.refresh_from_db()
        self.assertTrue(user.email_verified)
        self.assertIsNotNone(user.email_verified_at)
        self.assertEqual(mock_welcome.call_count, 1)

        # Re-hitting the link is idempotent — no second welcome email.
        r2 = APIClient().get(f"/api/auth/verify-email/?token={token}")
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(mock_welcome.call_count, 1)

    @patch("accounts.views.send_welcome_email")
    @patch("accounts.views.send_verification_email")
    def test_invalid_token_does_not_verify(self, _v, mock_welcome):
        self._register()
        r = APIClient().get("/api/auth/verify-email/?token=tampered.rubbish")
        self.assertEqual(r.status_code, 302)
        self.assertIn("verified=invalid", r.url)
        self.assertFalse(User.objects.get(email="newuser@test.com").email_verified)
        self.assertEqual(mock_welcome.call_count, 0)

    def test_token_roundtrip_and_rejects_garbage(self):
        self._register_ok = True
        user = User.objects.create_user(email="tok@test.com", password="x",
                                        first_name="T", last_name="K")
        token = make_verification_token(user)
        self.assertEqual(read_verification_token(token), str(user.id))
        self.assertIsNone(read_verification_token("not-a-real-token"))

    @patch("accounts.views.send_verification_email")
    def test_login_blocked_until_email_verified(self, _v):
        # Policy (VerifiedEmailTokenObtainPairSerializer): a correct password on
        # an unverified account gets an actionable, machine-readable refusal —
        # never a token, and never the generic "bad credentials" error.
        self._register()
        creds = {"email": "newuser@test.com", "password": "Signup#Pass123"}
        r = APIClient().post("/api/auth/token/", creds, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], ["email_not_verified"])
        self.assertNotIn("access", r.data)

        # Once verified, the same credentials sign in normally.
        User.objects.filter(email="newuser@test.com").update(email_verified=True)
        r = APIClient().post("/api/auth/token/", creds, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.data)

    def test_wrong_password_is_not_reported_as_unverified(self):
        # The verification check must not leak past a bad password: unknown
        # or wrong credentials still get simplejwt's standard 401.
        r = APIClient().post("/api/auth/token/", {
            "email": "nobody@test.com", "password": "whatever",
        }, format="json")
        self.assertEqual(r.status_code, 401)

    @patch("accounts.views.send_verification_email")
    def test_resend_is_generic_and_only_mails_unverified(self, mock_verify):
        self._register()  # unverified
        mock_verify.reset_mock()  # ignore the signup send; isolate resend behavior
        # unknown email → generic 200, no mail
        r0 = APIClient().post("/api/auth/resend-verification/",
                              {"email": "ghost@test.com"}, format="json")
        self.assertEqual(r0.status_code, 200)
        # known unverified → generic 200, mail sent
        r1 = APIClient().post("/api/auth/resend-verification/",
                              {"email": "newuser@test.com"}, format="json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r0.data["detail"], r1.data["detail"])   # identical message
        self.assertEqual(mock_verify.call_count, 1)              # only the real one

    @patch("accounts.views.send_verification_email")
    def test_email_verified_is_read_only_on_profile(self, _v):
        self._register()
        # Verified so login works; the point of this test is that a signed-in
        # user still can't flip the flag through their profile.
        User.objects.filter(email="newuser@test.com").update(email_verified=True)
        client = APIClient()
        tok = client.post("/api/auth/token/", {
            "email": "newuser@test.com", "password": "Signup#Pass123"}, format="json").data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
        # attempt to self-verify via profile update — must be ignored
        client.patch("/api/auth/me/", {"email_verified": False}, format="json")
        # Ignored: still True from the (server-side) verification above.
        self.assertTrue(User.objects.get(email="newuser@test.com").email_verified)


class TokenRevocationTests(TestCase):
    """The refresh token only travels in an httpOnly cookie; it rotates (the
    used one is revoked) and logout revokes it and clears the cookie."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        User.objects.create_user(email="rev@test.com", password="Str0ng!pass", email_verified=True)
        self.c = APIClient()
        r = self.c.post("/api/auth/token/", {"email": "rev@test.com", "password": "Str0ng!pass"}, format="json")
        self.assertIn("access", r.data)
        self.assertNotIn("refresh", r.data)  # never readable by page scripts
        cookie = r.cookies["azura_refresh"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual((cookie["path"], cookie["samesite"]), ("/api/auth/", "Strict"))
        self.refresh = cookie.value

    def test_refresh_rotates_and_logout_revokes(self):
        r = self.c.post("/api/auth/token/refresh/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.data)
        new = r.cookies["azura_refresh"].value
        self.assertNotEqual(new, self.refresh)
        # The used refresh token is dead: a stolen copy can't be replayed.
        stale = APIClient()
        stale.cookies["azura_refresh"] = self.refresh
        self.assertEqual(stale.post("/api/auth/token/refresh/").status_code, 401)
        # A token in the body is ignored: only the cookie counts.
        self.assertEqual(APIClient().post("/api/auth/token/refresh/", {"refresh": new}, format="json").status_code, 401)
        r = self.c.post("/api/auth/token/blacklist/")
        self.assertEqual((r.status_code, r.cookies["azura_refresh"].value), (200, ""))
        again = APIClient()
        again.cookies["azura_refresh"] = new
        self.assertEqual(again.post("/api/auth/token/refresh/").status_code, 401)


class LoginThrottleTests(TestCase):
    def test_login_is_rate_limited(self):
        from django.core.cache import cache
        cache.clear()
        codes = [self.client.post("/api/auth/token/", {"email": "x@example.com", "password": "wrong"}).status_code
                 for _ in range(11)]
        cache.clear()
        self.assertEqual(codes[-1], 429)
        self.assertNotIn(429, codes[:10])

    def test_a_forged_forwarded_ip_does_not_reset_the_limit(self):
        """Behind the two nginx hops (production NUM_PROXIES=2), only the
        address the host nginx saw counts; client-supplied entries don't."""
        from django.conf import settings
        from django.core.cache import cache
        cache.clear()
        with self.settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 2}):
            codes = [self.client.post("/api/auth/token/", {"email": "x@example.com", "password": "wrong"},
                                      HTTP_X_FORWARDED_FOR=f"10.9.9.{i}, 203.0.113.7, 172.18.0.1").status_code
                     for i in range(11)]
        cache.clear()
        self.assertEqual(codes[-1], 429)


class AdminSidebarPermissionTests(TestCase):
    def staff(self, *codenames):
        from django.contrib.auth.models import Permission
        user = User.objects.create_user(email="crm@test.com", password="x", is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.force_login(user)
        return User.objects.get(pk=user.pk)

    def test_crm_only_staff_see_and_open_only_the_crm(self):
        from types import SimpleNamespace
        from config.settings import _sidebar
        user = self.staff("view_lead", "change_lead", "view_chatthread", "change_chatthread")
        groups = _sidebar(SimpleNamespace(user=user))
        self.assertEqual([[str(i["title"]) for i in g["items"]] for g in groups],
                         [["Dashboard"], ["Leads & quotes", "Chat conversations", "Chat analytics"]])

        html = self.client.get("/admin/").content.decode()
        self.assertIn("/admin/bookings/lead/", html)
        for hidden in ("Newsletter", "Refunds", "Packages by Category"):
            self.assertNotIn(hidden, html)
        self.assertEqual(self.client.get("/admin/bookings/lead/").status_code, 200)
        self.assertEqual(self.client.get("/admin/reports/").status_code, 403)

    def test_superusers_keep_the_whole_menu(self):
        from types import SimpleNamespace
        from config.settings import SIDEBAR_GROUPS, _sidebar
        admin = User.objects.create_superuser(email="root@test.com", password="x")
        self.assertEqual(sum(len(g["items"]) for g in _sidebar(SimpleNamespace(user=admin))),
                         sum(len(g["items"]) for g in SIDEBAR_GROUPS))
        self.client.force_login(admin)
        self.assertIn("Packages by Category", self.client.get("/admin/").content.decode())
        self.assertEqual(self.client.get("/admin/reports/").status_code, 200)


class StaffManagingUsersTests(TestCase):
    """An account with user permissions can manage staff but not raise its own access."""

    def setUp(self):
        from django.contrib.auth.models import Permission
        self.staff = User.objects.create_user(email="manager@test.com", password="x", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            codename__in=["view_user", "add_user", "change_user", "delete_user"]))
        self.root = User.objects.create_superuser(email="root@test.com", password="root-pass")
        self.client.force_login(self.staff)

    def form(self, user, **extra):
        return {"email": user.email, "first_name": "", "last_name": "", "phone_number": "", "country": "",
                "is_active": "on", "is_staff": "on", **extra}

    def test_can_open_the_add_page(self):
        self.assertEqual(self.client.get("/admin/accounts/user/add/").status_code, 200)

    def test_cannot_make_itself_superuser_or_grant_permissions(self):
        from django.contrib.auth.models import Permission
        settings_perm = Permission.objects.get(codename="change_opsconfig")
        self.client.post(f"/admin/accounts/user/{self.staff.pk}/change/",
                         self.form(self.staff, is_superuser="on", user_permissions=[settings_perm.pk]))
        self.staff = User.objects.get(pk=self.staff.pk)
        self.assertFalse(self.staff.is_superuser)
        self.assertFalse(self.staff.has_perm("payments.change_opsconfig"))
        self.assertTrue(self.staff.has_perm("accounts.change_user"))  # the save didn't wipe its access

    def test_cannot_take_over_or_delete_a_superuser(self):
        self.assertEqual(self.client.post(f"/admin/accounts/user/{self.root.pk}/password/", {
            "password1": "Taken-Over-1", "password2": "Taken-Over-1"}).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/accounts/user/{self.root.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password("root-pass"))



class AdminModuleActionTests(TestCase):
    """Every module can be managed: activate, deactivate, publish, hide, reopen."""

    def setUp(self):
        self.root = User.objects.create_superuser(email="root@test.com", password="x")
        self.client.force_login(self.root)

    def act(self, url, action, *objects):
        return self.client.post(url, {"action": action, "index": 0, "_selected_action": [str(o.pk) for o in objects]}, follow=True)

    def test_users_are_deactivated_but_never_yourself(self):
        other = User.objects.create_user(email="other@test.com", password="x")
        self.act("/admin/accounts/user/", "deactivate_users", other, self.root)
        other.refresh_from_db()
        self.root.refresh_from_db()
        self.assertEqual((other.is_active, self.root.is_active), (False, True))
        self.act("/admin/accounts/user/", "activate_users", other)
        other.refresh_from_db()
        self.assertTrue(other.is_active)

    def test_newsletter_and_reviews(self):
        from bookings.tests import make_package
        from newsletter.models import NewsletterSubscriber
        from reviews.models import Review
        sub = NewsletterSubscriber.objects.create(email="n@test.com")
        self.act("/admin/newsletter/newslettersubscriber/", "deactivate", sub)
        sub.refresh_from_db()
        self.assertFalse(sub.is_active)
        self.assertEqual(self.act("/admin/newsletter/newslettersubscriber/", "export_csv", sub).status_code, 200)

        pkg = make_package()
        review = Review.objects.create(package=pkg, reviewer_name="A", reviewer_email="a@test.com", rating=1, body="x")
        self.assertEqual(self.client.get(f"/api/packages/{pkg.id}/reviews/").json()["count"], 1)
        self.act("/admin/reviews/review/", "hide_reviews", review)
        self.assertEqual(self.client.get(f"/api/packages/{pkg.id}/reviews/").json()["count"], 0)
        review.refresh_from_db()
        self.assertFalse(review.is_visible)  # kept on record

    def test_packages_and_trip_updates(self):
        from bookings.tests import make_package
        from packages.models import TripUpdate
        pkg = make_package()
        self.act("/admin/packages/travelpackage/", "deactivate_packages", pkg)
        pkg.refresh_from_db()
        self.assertFalse(pkg.is_active)
        update = TripUpdate.objects.create(package=pkg, title="T", body="B")
        self.act("/admin/packages/tripupdate/", "publish_updates", update)
        update.refresh_from_db()
        self.assertTrue(update.is_published)
        self.assertIsNotNone(update.published_at)

    def test_a_room_or_stay_in_use_cannot_be_deleted(self):
        from bookings.models import Booking
        from bookings.tests import make_package
        from packages.models import RoomUnit, StayType
        pkg = make_package()
        stay = StayType.objects.create(package=pkg, code="s", name="S", kind="airbnb", capacity=2, price_per_night=1)
        free = RoomUnit.objects.create(stay_type=stay, label="free")
        used = RoomUnit.objects.create(stay_type=stay, label="used")
        self.assertEqual(self.client.get(f"/admin/packages/roomunit/{free.pk}/delete/").status_code, 200)
        booking = Booking.objects.create(package=pkg, first_name="A", last_name="B", email="a@test.com", num_guests=1,
                                         travel_date="2027-01-06", unit_price=1, total_amount=1, currency="USD")
        RoomUnit.objects.filter(pk=used.pk).update(booking=booking)
        self.assertEqual(self.client.post(f"/admin/packages/roomunit/{used.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/packages/staytype/{stay.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertTrue(RoomUnit.objects.filter(pk=used.pk).exists())

    def test_lost_leads_and_closed_chats_reopen_and_tasks_pause(self):
        from bookings.models import ChatThread, Lead
        from payments.models import ScheduledTask
        lead = Lead.objects.create(name="A", email="a@test.com", stage="lost")
        chat = ChatThread.objects.create(wa_id="233200000000", status="closed")
        task = ScheduledTask.objects.create(name="refresh_fx_rates")
        self.act("/admin/bookings/lead/", "reopen", lead)
        self.act("/admin/bookings/chatthread/", "reopen", chat)
        self.act("/admin/payments/scheduledtask/", "pause", task)
        lead.refresh_from_db(); chat.refresh_from_db(); task.refresh_from_db()
        self.assertEqual((lead.stage, chat.status, task.is_enabled), ("new", "open", False))

    def test_view_only_staff_do_not_get_the_actions(self):
        from django.contrib.auth.models import Permission
        viewer = User.objects.create_user(email="v@test.com", password="x", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_user"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get("/admin/accounts/user/"), "deactivate_users")
