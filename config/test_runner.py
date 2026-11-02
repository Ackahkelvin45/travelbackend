"""Tests start with an empty cache: API throttles count there, so otherwise a
run's result depends on test order (a shuffled run can hit 429s)."""

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner


class CleanCacheRunner(DiscoverRunner):
    def run_tests(self, *args, **kwargs):
        original = SimpleTestCase._setup_and_call

        def setup_and_call(test, *a, **kw):
            cache.clear()
            return original(test, *a, **kw)

        SimpleTestCase._setup_and_call = setup_and_call
        try:
            return super().run_tests(*args, **kwargs)
        finally:
            SimpleTestCase._setup_and_call = original
