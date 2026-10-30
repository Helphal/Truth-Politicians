"""Bootstrap de capacité : deux comptes initiaux (admin + éditeur), hors secrets Git.

Les pseudonymes sont générés à l'exécution. Les mots de passe temporaires sont
affichés une seule fois sur stdout ; jamais journalisés dans Git.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from atelier.models import UserProfile
from atelier.services.accounts import create_account


class Command(BaseCommand):
    help = "Crée deux comptes initiaux (admin + éditeur) avec pseudonymes générés."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Autorise la création même si des comptes admin existent déjà.",
        )
        parser.add_argument(
            "--invite",
            action="store_true",
            help="Utilise des jetons d'invitation au lieu de mots de passe temporaires.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        existing_admins = UserProfile.objects.filter(role=UserProfile.Role.ADMIN).count()
        if existing_admins and not options["force"]:
            raise CommandError("bootstrap_refused_admin_already_exists")
        admin = create_account(
            role=UserProfile.Role.ADMIN,
            actor=None,
            is_test_identity=False,
            use_invite_token=options["invite"],
        )
        editor = create_account(
            role=UserProfile.Role.EDITOR,
            actor=None,
            is_test_identity=False,
            use_invite_token=options["invite"],
        )
        mask = os.environ.get("DV_ENV", "").strip().lower() == "test"
        self.stdout.write(self.style.SUCCESS("bootstrap_initial_accounts_ok"))
        if mask:
            self.stdout.write("bootstrap_stdout_redacted=1")
            self.stdout.write("admin_login=[REDACTED]")
            self.stdout.write(f"admin_internal_uid={admin.internal_uid}")
            self.stdout.write("admin_temporary_password=[REDACTED]")
            self.stdout.write("editor_login=[REDACTED]")
            self.stdout.write(f"editor_internal_uid={editor.internal_uid}")
            self.stdout.write("editor_temporary_password=[REDACTED]")
        else:
            self.stdout.write(f"admin_login={admin.login_pseudonym}")
            self.stdout.write(f"admin_internal_uid={admin.internal_uid}")
            if admin.temporary_password:
                self.stdout.write(f"admin_temporary_password={admin.temporary_password}")
            if admin.invite_token:
                self.stdout.write(f"admin_invite_token={admin.invite_token}")
            self.stdout.write(f"editor_login={editor.login_pseudonym}")
            self.stdout.write(f"editor_internal_uid={editor.internal_uid}")
            if editor.temporary_password:
                self.stdout.write(f"editor_temporary_password={editor.temporary_password}")
            if editor.invite_token:
                self.stdout.write(f"editor_invite_token={editor.invite_token}")
            self.stdout.write("NOTE: transmettre ces accès hors Git ; changement obligatoire au premier login.")
