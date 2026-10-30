from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('atelier', '0035_deterministic_provenance')]

    operations = [
        migrations.AddField(
            model_name='editorialrevision', name='conclusion_contract',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='proofartifact', name='kind',
            field=models.CharField(max_length=32, choices=[(value, value) for value in (
                'preflight', 'semantic', 'counterevidence', 'assessment', 'editorial',
                'rights', 'dependency_graph')]),
        ),
    ]
