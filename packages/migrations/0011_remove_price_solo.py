from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0010_add_price_solo"),
    ]

    operations = [
        migrations.RemoveField(model_name="travelpackage", name="price_solo"),
    ]
