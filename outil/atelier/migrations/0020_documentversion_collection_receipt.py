from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("atelier", "0019_research_preparation")]

    operations = [
        migrations.AddField(
            model_name="documentversion",
            name="collection_receipt",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
