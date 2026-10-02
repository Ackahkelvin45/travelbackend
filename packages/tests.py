from django.core.management import call_command
from django.test import TestCase


class PublicPackageEndpointsTests(TestCase):
    """Smoke check: the public package pages must render for a seeded package."""

    def test_list_trending_detail_and_pricing_respond(self):
        call_command("seed_michael_blackson", verbosity=0, stdout=open("/dev/null", "w"))
        listing = self.client.get("/api/packages/")
        self.assertEqual(listing.status_code, 200)
        package_id = listing.json()["results"][0]["id"]
        for url in ("/api/packages/trending/", f"/api/packages/{package_id}/",
                    f"/api/packages/{package_id}/pricing/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
