# Generated manually for BUILD-002-R5 detail work queue

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("atelier", "0024_build002_r1_execution_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="DetailWorkItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("feed_id", models.CharField(db_index=True, max_length=120)),
                ("parser_id", models.CharField(max_length=80)),
                ("canonical_url", models.URLField(max_length=1000)),
                ("work_key", models.CharField(max_length=64, unique=True)),
                ("title", models.CharField(blank=True, max_length=500)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "En attente"),
                            ("fetched", "Fetché"),
                            ("parsed", "Parsé (discours extrait)"),
                            ("incomplete", "Incomplet (pas de citation démontrée)"),
                            ("retryable_error", "Erreur réessayable"),
                            ("blocked_permanent", "Bloqué permanent"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveIntegerField(default=5)),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, max_length=120)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("execution_mode", models.CharField(blank=True, max_length=32)),
                ("meta", models.JSONField(blank=True, default=dict)),
                (
                    "detail_document",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="detail_work_as_evidence",
                        to="atelier.documentversion",
                    ),
                ),
                (
                    "parent_document",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="detail_work_children",
                        to="atelier.documentversion",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddIndex(
            model_name="detailworkitem",
            index=models.Index(fields=["feed_id", "status", "next_attempt_at"], name="dv_detail_work_due_idx"),
        ),
    ]
