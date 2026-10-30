from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("atelier", "0028_build003_accounts_r2"),
    ]

    operations = [
        migrations.AddField(
            model_name="humanreview",
            name="authority_internal_uid",
            field=models.CharField(blank=True, default="", editable=False, max_length=36),
        ),
    ]
