"""Shared admin helpers."""

import csv
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def post_only(view):
    """A detail button that changes something: runs only from the confirmed
    POST (admin-aurora.js), so a pasted link or a crawler can't trigger it."""
    @wraps(view)
    def inner(self, request, object_id):
        if request.method != "POST":
            self.message_user(request, "Use the button on this page to do that.", messages.WARNING)
            opts = self.model._meta
            return redirect(f"admin:{opts.app_label}_{opts.model_name}_change", object_id)
        return view(self, request, object_id)
    return inner


class _SafeWriter:
    """A cell starting with = + - or @ runs as a formula in Excel, and these
    exports hold names guests typed themselves."""

    def __init__(self, response):
        self.writer = csv.writer(response)

    def _clean(self, cell):
        return f"'{cell}" if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@") else cell

    def writerow(self, row):
        self.writer.writerow([self._clean(c) for c in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def safe_csv(response):
    return _SafeWriter(response)
