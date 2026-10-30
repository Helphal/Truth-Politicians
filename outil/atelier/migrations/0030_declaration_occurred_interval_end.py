from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("atelier", "0029_racc001_review_authority_binding"),
    ]

    operations = [
        migrations.AddField(
            model_name="declaration",
            name="occurred_interval_end",
            field=models.DateField(
                blank=True,
                help_text="Borne finale qualifiée d'un intervalle documentaire (distincte de published_on).",
                null=True,
            ),
        ),
    ]
