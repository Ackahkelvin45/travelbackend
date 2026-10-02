"""
Publish the approved customer policy texts (bookings/policy_texts.py) as the
current versions. Idempotent: a type already published at that VERSION is left
alone (published documents are immutable). Run after deploy.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import PolicyDocument
from bookings.policy_texts import DOCUMENTS, VERSION


class Command(BaseCommand):
    help = f"Publish policy texts v{VERSION} as the current documents (idempotent)."

    def handle(self, *args, **options):
        for doc_type, title, body, required in DOCUMENTS:
            existing = PolicyDocument.objects.filter(type=doc_type, version=VERSION).first()
            if existing:
                if not existing.is_current:
                    existing.is_current = True
                    existing.save()
                self.stdout.write(f"= {title} v{VERSION} already published")
                continue
            PolicyDocument.objects.create(
                type=doc_type, version=VERSION, title=title, body=body.strip(),
                is_required=required, is_current=True, published_at=timezone.now(),
            )
            self.stdout.write(self.style.SUCCESS(f"✓ {title} v{VERSION} published{'' if required else ' (optional)'}"))
