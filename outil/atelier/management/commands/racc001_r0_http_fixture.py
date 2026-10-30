"""Prépare la base TEST du service HTTP R0 (comptes et dossier synthétique)."""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from atelier.models import EditorialDossier, UserProfile
from atelier.services.accounts import create_account
from atelier.tests.support import AtelierTestCase


class Command(BaseCommand):
    help = "Fixture R0 : titulaire, éditeur, admin non titulaire, suspendu, dossier speech_vote."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True, help="Chemin JSON de sortie (dossier runtime livraison).")

    def handle(self, *args, **options):
        manifest_path = Path(options["manifest"]).resolve()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        holder_pw = secrets.token_urlsafe(24)
        editor_pw = secrets.token_urlsafe(24)
        admin_other_pw = secrets.token_urlsafe(24)
        suspended_pw = secrets.token_urlsafe(24)
        session_pw = secrets.token_urlsafe(24)

        holder_cred = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        holder = get_user_model().objects.get(pk=holder_cred.user_id)
        holder.set_password(holder_pw)
        holder.dv_profile.must_change_password = False
        holder.dv_profile.save()
        holder.save()

        editor_cred = create_account(role=UserProfile.Role.EDITOR, actor=holder, is_test_identity=True)
        editor = get_user_model().objects.get(pk=editor_cred.user_id)
        editor.set_password(editor_pw)
        editor.dv_profile.must_change_password = False
        editor.dv_profile.save()
        editor.save()

        admin_other_cred = create_account(role=UserProfile.Role.ADMIN, actor=holder, is_test_identity=True)
        admin_other = get_user_model().objects.get(pk=admin_other_cred.user_id)
        admin_other.set_password(admin_other_pw)
        admin_other.dv_profile.must_change_password = False
        admin_other.dv_profile.save()
        admin_other.save()

        suspended_cred = create_account(role=UserProfile.Role.REVIEWER, actor=holder, is_test_identity=True)
        suspended = get_user_model().objects.get(pk=suspended_cred.user_id)
        suspended.set_password(suspended_pw)
        suspended.is_active = False
        suspended.dv_profile.active = False
        suspended.dv_profile.account_status = UserProfile.AccountStatus.SUSPENDED
        suspended.dv_profile.must_change_password = False
        suspended.dv_profile.save()
        suspended.save()

        session_cred = create_account(role=UserProfile.Role.REVIEWER, actor=holder, is_test_identity=True)
        session_user = get_user_model().objects.get(pk=session_cred.user_id)
        session_user.set_password(session_pw)
        session_user.dv_profile.must_change_password = False
        session_user.dv_profile.save()
        session_user.save()

        harness = AtelierTestCase()
        harness.setUp()
        dossier, revision, _ = harness.make_revision(
            EditorialDossier.Kind.SPEECH_VOTE,
            environment="test",
            data_scope="synthetic",
        )
        manifest = {
            "marker": "DV_RACC_001_R0_SERVICE_HTTP",
            "dossier_id": dossier.pk,
            "dossier_stable_id": dossier.stable_id,
            "revision": revision.revision,
            "snapshot_digest": revision.snapshot_digest,
            "holder_username": holder.username,
            "editor_username": editor.username,
            "admin_other_username": admin_other.username,
            "suspended_username": suspended.username,
            "holder_internal_uid": str(holder.dv_profile.internal_uid),
            "admin_other_internal_uid": str(admin_other.dv_profile.internal_uid),
            "holder_password": holder_pw,
            "editor_password": editor_pw,
            "admin_other_password": admin_other_pw,
            "suspended_password": suspended_pw,
            "session_then_suspend_username": session_user.username,
            "session_then_suspend_password": session_pw,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "manifest": str(manifest_path),
                    "holder_internal_uid": manifest["holder_internal_uid"],
                    "dossier_id": dossier.pk,
                }
            )
        )
