"""
Create/refresh the three Azura staff permission groups. Idempotent — run after
every deploy (`manage.py setup_staff_groups`). The Django admin AND the staff
portal enforce exactly these permissions, so this file is the single place the
"who can do what" table lives.

  Viewer      read bookings, payments, refunds, cancellation requests
  Operations  + record offline payments, extend deadlines, mark arrived,
                approve/reject cancellation requests, see special requests
  Finance     + mark refunds processed, cancel bookings (guest bands),
                cancel as organizer (full refund)

Staff accounts also need is_staff=True to open /admin (set on the user).
Developers/owners stay superusers and are unaffected.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEWER = [
    "bookings.view_booking", "bookings.view_policyacceptance", "bookings.view_cancellationrequest",
    "payments.view_payment", "payments.view_refund",
]
OPERATIONS = VIEWER + [
    "bookings.change_booking",            # open the change form / save inlines
    "bookings.view_special_requests",     # dietary / access needs — restricted
    "bookings.extend_deadline",
    "bookings.mark_arrived",
    "bookings.change_cancellationrequest",  # staff note
    "bookings.resolve_cancellation",
    "payments.add_payment",               # offline payment inline → services
]
FINANCE = OPERATIONS + [
    "payments.change_refund",
    "payments.process_refund",
    "bookings.cancel_booking",
    "bookings.cancel_as_organizer",
]

GROUPS = {
    "Azura Viewer": VIEWER,
    "Azura Operations": OPERATIONS,
    "Azura Finance": FINANCE,
}


def _perm(label: str) -> Permission:
    app_label, codename = label.split(".", 1)
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


class Command(BaseCommand):
    help = "Create or refresh the Azura Viewer / Operations / Finance staff groups (idempotent)."

    def handle(self, *args, **options):
        for name, labels in GROUPS.items():
            group, _ = Group.objects.get_or_create(name=name)
            group.permissions.set([_perm(l) for l in labels])
            self.stdout.write(self.style.SUCCESS(f"{name}: {len(labels)} permission(s)"))
