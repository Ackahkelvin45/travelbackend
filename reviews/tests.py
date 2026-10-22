from django.core.cache import cache
from django.test import TestCase

from bookings.serializers import ConfigurableCheckoutSerializer
from bookings.tests import make_package

from .models import Review


class ReviewHardeningTests(TestCase):
    """Anonymous reviews are stored as plain text and capped against spam."""

    def setUp(self):
        cache.clear()
        self.url = f"/api/packages/{make_package().id}/reviews/"
        self.body = {"reviewer_name": "<b>Efua</b>", "reviewer_email": "efua@example.com", "rating": 5,
                     "title": "Great", "body": "Loved it <script>alert(1)</script><img src=x onerror=alert(1)>"}

    def test_markup_is_stripped_and_posting_is_throttled(self):
        self.assertEqual(self.client.post(self.url, self.body, content_type="application/json").status_code, 201)
        r = Review.objects.get()
        self.assertEqual((r.reviewer_name, r.body), ("Efua", "Loved it alert(1)"))
        self.assertEqual(self.client.post(self.url, {**self.body, "body": "<i></i>"},
                                          content_type="application/json").status_code, 400)
        for _ in range(4):
            self.client.post(self.url, self.body, content_type="application/json")
        self.assertEqual(self.client.post(self.url, self.body, content_type="application/json").status_code, 429)
        self.assertEqual(self.client.get(self.url).status_code, 200)  # reading is never throttled

    def test_guest_count_is_capped(self):
        s = ConfigurableCheckoutSerializer(data={"package_id": "00000000-0000-0000-0000-000000000000", "num_guests": 500})
        s.is_valid()
        self.assertIn("num_guests", s.errors)
