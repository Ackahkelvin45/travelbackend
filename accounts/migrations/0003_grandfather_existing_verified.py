from django.db import migrations


def mark_existing_verified(apps, schema_editor):
    """Existing accounts predate email verification — treat them as already
    verified so the rollout never locks anyone out or re-emails them."""
    User = apps.get_model("accounts", "User")
    User.objects.filter(email_verified=False).update(email_verified=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_email_verified_user_email_verified_at"),
    ]

    operations = [
        migrations.RunPython(mark_existing_verified, noop_reverse),
    ]
