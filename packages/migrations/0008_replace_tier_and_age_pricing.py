from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0007_price_tiers"),
    ]

    operations = [
        # Remove tier
        migrations.RemoveField(model_name="travelpackage", name="tier"),

        # Remove age-based pricing
        migrations.RemoveField(model_name="travelpackage", name="price_adult"),
        migrations.RemoveField(model_name="travelpackage", name="price_youth"),
        migrations.RemoveField(model_name="travelpackage", name="price_children"),

        # Add accommodation-based pricing
        migrations.AddField(
            model_name="travelpackage",
            name="price_shared",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Shared / Couples — starting price (Best Value)",
                max_digits=12,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_shared_max",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Shared / Couples — upper end of range (leave blank for a single price)",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_private",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Private / Solo — starting price",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_private_max",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Private / Solo — upper end of range",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_vip",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="VIP — starting price (open-ended, displayed as '$X+')",
                max_digits=12,
                null=True,
            ),
        ),
    ]
