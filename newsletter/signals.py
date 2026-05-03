from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from packages.models import TravelPackage

from .services import send_new_package_announcement


@receiver(post_save, sender=TravelPackage)
def send_package_newsletter_announcement(sender, instance, created, raw, **kwargs):
    if raw or not created:
        return

    transaction.on_commit(
        lambda: send_new_package_announcement(package_id=instance.id)
    )

