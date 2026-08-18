from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.emails import make_verification_token, read_verification_token
from accounts.models import User


@override_settings(FRONTEND_URL="https://azuratravels.live", RESEND_API_KEY="re_test_key")
class SignupVerificationTests(TestCase):
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
    def test_login_not_blocked_for_unverified_user(self, _v):
        self._register()
        r = APIClient().post("/api/auth/token/", {
            "email": "newuser@test.com", "password": "Signup#Pass123",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.data)

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
        client = APIClient()
        tok = client.post("/api/auth/token/", {
            "email": "newuser@test.com", "password": "Signup#Pass123"}, format="json").data["access"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
        # attempt to self-verify via profile update — must be ignored
        client.patch("/api/auth/me/", {"email_verified": True}, format="json")
        self.assertFalse(User.objects.get(email="newuser@test.com").email_verified)
