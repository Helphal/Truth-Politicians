from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('atelier', '0036_conclusion_dependency_receipts')]
    operations = [migrations.CreateModel(
        name='ScrutinyTextBinding',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('version', models.PositiveIntegerField()),
            ('procedure', models.CharField(max_length=250)),
            ('actor_id', models.PositiveIntegerField()),
            ('payload', models.JSONField()),
            ('payload_digest', models.CharField(max_length=64)),
            ('scrutiny', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name='text_bindings', to='atelier.scrutiny')),
            ('source_document', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name='scrutiny_text_bindings', to='atelier.documentversion')),
            ('submitted_text', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name='scrutiny_bindings', to='atelier.textversion')),
        ],
        options={'constraints': [models.UniqueConstraint(fields=('scrutiny', 'version'),
                name='dv_unique_scrutiny_text_version')],
            'indexes': [models.Index(fields=['scrutiny', 'payload_digest'], name='dv_scrutiny_text_digest')]},
    )]
