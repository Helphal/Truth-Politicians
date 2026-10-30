import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def populate_profile_identity(apps, schema_editor):
    UserProfile = apps.get_model("atelier", "UserProfile")
    for profile in UserProfile.objects.all():
        if not profile.internal_uid:
            profile.internal_uid = uuid.uuid4()
        username = ""
        try:
            username = profile.user.username
        except Exception:
            username = f"legacy-{profile.pk}"
        if not profile.login_pseudonym:
            profile.login_pseudonym = username or f"legacy-{profile.pk}"
        if not profile.public_attribution_label:
            profile.public_attribution_label = profile.login_pseudonym
        if not profile.account_status:
            profile.account_status = "active" if profile.active else "suspended"
        profile.save()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("atelier", "0025_build002_r5_detail_work"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="account_status",
            field=models.CharField(
                choices=[
                    ("active", "Actif"),
                    ("suspended", "Suspendu"),
                    ("revoked", "Révoqué"),
                ],
                default="active",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="internal_uid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="last_activity_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="login_pseudonym",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="must_change_password",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="permissions_extra",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="public_attribution_label",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Libellé d'attribution éditoriale ; jamais l'identifiant technique Site par défaut.",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="session_epoch",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "Administrateur"),
                    ("editor", "Éditeur"),
                    ("reviewer", "Relecteur humain"),
                    ("operator", "Opérateur"),
                    ("collector", "Compte de collecte"),
                ],
                max_length=24,
            ),
        ),
        migrations.RunPython(populate_profile_identity, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="userprofile",
            name="internal_uid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="login_pseudonym",
            field=models.CharField(blank=True, default="", max_length=80, unique=True),
        ),
        migrations.CreateModel(
            name="AccountInviteToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="dv_invites_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dv_invite_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["expires_at", "used_at"], name="atelier_acc_expires_d5740c_idx")],
            },
        ),
    ]
