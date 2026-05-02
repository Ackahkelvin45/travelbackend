from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0006_add_whats_included_and_location"),
    ]

    operations = [
        migrations.RenameField(
            model_name="travelpackage",
            old_name="price_per_person",
            new_name="price_adult",
        ),
        migrations.AlterField(
            model_name="travelpackage",
            name="price_adult",
            field=models.DecimalField(
                decimal_places=2,
                help_text="Price per adult (18+ years)",
                max_digits=12,
            ),
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_youth",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Price per youth (13–17 years); leave blank to match adult price",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="travelpackage",
            name="price_children",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Price per child (0–12 years); leave blank to match adult price",
                max_digits=12,
                null=True,
            ),
        ),
    ]
