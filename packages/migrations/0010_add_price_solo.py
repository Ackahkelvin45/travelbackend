from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0009_remove_price_max_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="travelpackage",
            name="price_solo",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Solo price",
                max_digits=12,
                null=True,
            ),
        ),
    ]
