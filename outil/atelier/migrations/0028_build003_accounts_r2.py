"""BUILD-003 R2 : fenêtre temporelle LoginThrottle (sans toucher au trigger UID)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("atelier", "0027_build003_accounts_r1"),
    ]

    operations = [
        migrations.AddField(
            model_name="loginthrottle",
            name="window_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
