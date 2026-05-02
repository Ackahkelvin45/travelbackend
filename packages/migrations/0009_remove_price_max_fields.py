from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0008_replace_tier_and_age_pricing"),
    ]

    operations = [
        migrations.RemoveField(model_name="travelpackage", name="price_shared_max"),
        migrations.RemoveField(model_name="travelpackage", name="price_private_max"),
    ]
