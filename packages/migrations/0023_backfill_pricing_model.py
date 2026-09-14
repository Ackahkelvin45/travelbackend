from django.db import migrations


def set_pricing_model(apps, schema_editor):
    """Existing packages predate the pricing_model field (which defaults to
    'flat'). Flip packages that sell via hotel options to 'option_based' so the
    flagship keeps its current pricing path. Day tours / legacy tiers stay flat."""
    TravelPackage = apps.get_model("packages", "TravelPackage")
    for pkg in TravelPackage.objects.all():
        if pkg.options.exists():
            pkg.pricing_model = "option_based"
            pkg.save(update_fields=["pricing_model"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0022_travelpackage_base_price_per_person_and_more"),
    ]

    operations = [
        migrations.RunPython(set_pricing_model, noop),
    ]
