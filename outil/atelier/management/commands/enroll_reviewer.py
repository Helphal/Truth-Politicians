"""Enrôlement interactif d'un relecteur réel, sans secret dans les arguments."""
from __future__ import annotations

import getpass
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from atelier.models import UserProfile


class Command(BaseCommand):
    help = "Crée un compte de revue privé par saisie interactive hors journaux."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.DV_ENV != "private":
            raise CommandError("reviewer_enrollment_private_only")
        if not sys.stdin.isatty():
            raise CommandError("reviewer_enrollment_interactive_tty_required")
        username = options["username"].strip()
        if not username or len(username) > 150:
            raise CommandError("reviewer_username_invalid")
        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            raise CommandError("reviewer_account_already_exists")
        password = getpass.getpass("Mot de passe personnel (non journalisé) : ")
        confirmation = getpass.getpass("Confirmation : ")
        if password != confirmation:
            raise CommandError("reviewer_password_confirmation_mismatch")
        candidate = user_model(username=username)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError("reviewer_password_policy_rejected") from exc
        candidate.set_password(password)
        candidate.is_staff = False
        candidate.is_superuser = False
        candidate.save()
        UserProfile.objects.create(
            user=candidate,
            role=UserProfile.Role.REVIEWER,
            active=True,
            is_test_identity=False,
            login_pseudonym=username,
            public_attribution_label=username,
            account_status=UserProfile.AccountStatus.ACTIVE,
            must_change_password=False,
        )
        self.stdout.write(self.style.SUCCESS(f"reviewer_enrolled={username} role=reviewer test_identity=false"))
