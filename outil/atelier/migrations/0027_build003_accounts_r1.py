"""BUILD-003 R1 : throttle sans réseau persisté + garde internal_uid SQLite."""

from django.db import migrations


TRIGGER_NAME = "atelier_userprofile_internal_uid_immutable"


def clear_legacy_login_throttles(apps, schema_editor):
    """Invalide les anciens digests HMAC(IP|username) de recette — sans migration de valeur."""
    LoginThrottle = apps.get_model("atelier", "LoginThrottle")
    LoginThrottle.objects.all().delete()


def install_internal_uid_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME}")
        cursor.execute(
            f"""
            CREATE TRIGGER {TRIGGER_NAME}
            BEFORE UPDATE OF internal_uid ON atelier_userprofile
            FOR EACH ROW
            WHEN OLD.internal_uid IS NOT NULL
                 AND NEW.internal_uid IS NOT OLD.internal_uid
            BEGIN
                SELECT RAISE(ABORT, 'internal_uid_immutable');
            END;
            """
        )


def drop_internal_uid_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME}")


class Migration(migrations.Migration):

    dependencies = [
        ("atelier", "0026_build003_multi_accounts"),
    ]

    operations = [
        migrations.RunPython(clear_legacy_login_throttles, migrations.RunPython.noop),
        migrations.RunPython(install_internal_uid_guard, drop_internal_uid_guard),
    ]
