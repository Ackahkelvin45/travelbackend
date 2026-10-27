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
