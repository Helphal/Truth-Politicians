"""Preuves HTTP contre un service Outil réel (pas le client de test Django)."""
from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from atelier.models import EditorialDossier, EditorialRevision, HumanReview, OutboxEvent, UserProfile
from atelier.services.racc001_loopback_http_client import (
    MISSION_HTTP_ORIGIN,
    Racc001LoopbackHttpClient,
    _assert_mission_url,
    login_page_visible,
    session_authenticated,
)


def _refusal_evidence(status: int, body: str) -> bool:
    if status >= 500:
        return False
    if status in {401, 403}:
        return True
    return login_page_visible(body)


class Command(BaseCommand):
    help = "GET/POST HTTP réels contre runserver R0 (refus éditeur/admin/suspendu, approbation titulaire)."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default=MISSION_HTTP_ORIGIN)
        parser.add_argument("--manifest", required=True)

    def _semantic_snapshot(self, dossier_id: int, revision_id: int) -> dict:
        dossier = EditorialDossier.objects.get(pk=dossier_id)
        revision = EditorialRevision.objects.get(pk=revision_id)
        reviews = list(
            HumanReview.objects.filter(revision_id=revision_id)
            .order_by("pk")
            .values("pk", "decision", "reviewer_id", "reviewed_digest", "authority_internal_uid")
        )
        outbox = list(
            OutboxEvent.objects.filter(dossier_id=dossier_id)
            .order_by("pk")
            .values("pk", "operation", "sequence", "state")
        )
        return {
            "dossier_state": dossier.state,
            "dossier_suspended": dossier.suspended,
            "current_revision": dossier.current_revision,
            "revision_number": revision.revision,
            "snapshot_digest": revision.snapshot_digest,
            "revision_title": revision.title,
            "reviews": reviews,
            "outbox": outbox,
        }

    def handle(self, *args, **options):
        manifest = json.loads(Path(options["manifest"]).read_text(encoding="utf-8"))
        base = options["base_url"]
        _assert_mission_url(base)
        dossier_id = manifest["dossier_id"]
        lines: list[str] = []

        probe = Racc001LoopbackHttpClient(base)
        status, body = probe.request("GET", "/sante/")
        lines.append(f"GET /sante/ status={status}")
        if status != 200 or "ok" not in body:
            raise CommandError("health_not_ok")
        lines.append("health_ok")

        dossier = EditorialDossier.objects.get(pk=dossier_id)
        revision = dossier.revisions.get(revision=manifest["revision"])
        state_before = dossier.state
        baseline = self._semantic_snapshot(dossier_id, revision.pk)

        editor = Racc001LoopbackHttpClient(base)
        status, authed = editor.login_outcome(manifest["editor_username"], manifest["editor_password"])
        lines.append(f"login editor status={status} authed={authed}")
        if not authed:
            raise CommandError("editor_login_failed")
        csrf = editor.csrf_from_page(f"/dossiers/{dossier_id}/")
        review_path = f"/dossiers/{dossier_id}/"
        status, editor_body = editor.request(
            "POST",
            f"/dossiers/{dossier_id}/action/",
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "review",
                "expected_revision": str(manifest["revision"]),
                "expected_digest": manifest["snapshot_digest"],
                "decision": "approve",
                "note": "R0 HTTP proof editor refuse",
            },
            referer_path=review_path,
        )
        lines.append(f"POST editor approve status={status}")
        dossier.refresh_from_db()
        if dossier.state != state_before:
            raise CommandError("editor_mutated_dossier_state")
        if self._semantic_snapshot(dossier_id, revision.pk) != baseline:
            raise CommandError("editor_mutated_semantic_state")
        if not _refusal_evidence(status, editor_body):
            raise CommandError(f"editor_expected_forbidden got={status}")
        lines.append("editor_refused_no_mutation_ok")

        admin_other = Racc001LoopbackHttpClient(base)
        status, authed = admin_other.login_outcome(manifest["admin_other_username"], manifest["admin_other_password"])
        lines.append(f"login admin_non_holder status={status} authed={authed}")
        if not authed:
            raise CommandError("admin_other_login_failed")
        csrf = admin_other.csrf_from_page(f"/dossiers/{dossier_id}/")
        status, admin_body = admin_other.request(
            "POST",
            f"/dossiers/{dossier_id}/action/",
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "review",
                "expected_revision": str(manifest["revision"]),
                "expected_digest": manifest["snapshot_digest"],
                "decision": "approve",
                "note": "R0 HTTP proof admin non titulaire",
            },
            referer_path=review_path,
        )
        lines.append(f"POST admin_non_holder approve status={status}")
        dossier.refresh_from_db()
        if dossier.state != state_before:
            raise CommandError("admin_other_mutated_dossier_state")
        if self._semantic_snapshot(dossier_id, revision.pk) != baseline:
            raise CommandError("admin_other_mutated_semantic_state")
        if not _refusal_evidence(status, admin_body):
            raise CommandError(f"admin_other_expected_forbidden got={status}")
        lines.append("admin_non_holder_refused_no_mutation_ok")

        suspended = Racc001LoopbackHttpClient(base)
        status, authed = suspended.login_outcome(
            manifest["suspended_username"], manifest["suspended_password"]
        )
        _, login_body = suspended.request("GET", "/connexion/")
        lines.append(f"login suspended status={status} authed={authed}")
        if authed:
            raise CommandError("suspended_login_should_not_authenticate")
        if not _refusal_evidence(status, login_body):
            raise CommandError(f"suspended_login_expected_auth_refusal got={status}")
        lines.append("suspended_login_refused_ok")
        if self._semantic_snapshot(dossier_id, revision.pk) != baseline:
            raise CommandError("suspended_login_mutated_semantic_state")

        session_name = manifest.get("session_then_suspend_username")
        session_password = manifest.get("session_then_suspend_password")
        if not session_name or not session_password:
            raise CommandError("session_suspend_scenario_required")
        session_client = Racc001LoopbackHttpClient(base)
        status, authed = session_client.login_outcome(session_name, session_password)
        lines.append(f"login session_user status={status} authed={authed}")
        if not authed:
            raise CommandError("session_user_login_failed")
        user = get_user_model().objects.get(username=session_name)
        user.is_active = False
        user.dv_profile.active = False
        user.dv_profile.account_status = UserProfile.AccountStatus.SUSPENDED
        user.dv_profile.save()
        user.save()
        csrf = session_client.csrf_from_page(f"/dossiers/{dossier_id}/")
        status, session_body = session_client.request(
            "POST",
            f"/dossiers/{dossier_id}/action/",
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "review",
                "expected_revision": str(manifest["revision"]),
                "expected_digest": manifest["snapshot_digest"],
                "decision": "approve",
                "note": "R0 HTTP proof session suspendue",
            },
            referer_path=review_path,
        )
        lines.append(f"POST session_suspended approve status={status}")
        if self._semantic_snapshot(dossier_id, revision.pk) != baseline:
            raise CommandError("session_suspended_mutated_semantic_state")
        if not _refusal_evidence(status, session_body):
            raise CommandError(f"session_suspended_expected_refusal got={status}")
        home_status, home_body = session_client.request("GET", "/")
        if home_status >= 500:
            raise CommandError(f"session_suspended_home_server_error status={home_status}")
        if session_authenticated(home_body):
            raise CommandError("session_suspended_still_authenticated")
        if not _refusal_evidence(home_status, home_body) and home_status not in {200}:
            raise CommandError(f"session_suspended_home_unexpected status={home_status}")
        lines.append("session_suspended_refused_deauthenticated_ok")

        holder = Racc001LoopbackHttpClient(base)
        status, authed = holder.login_outcome(manifest["holder_username"], manifest["holder_password"])
        lines.append(f"login holder status={status} authed={authed}")
        if not authed:
            raise CommandError("holder_login_failed")

        reviews_before_ids = set(
            HumanReview.objects.filter(revision=revision).values_list("pk", flat=True)
        )
        csrf = holder.csrf_from_page(f"/dossiers/{dossier_id}/")
        status, _ = holder.request(
            "POST",
            f"/dossiers/{dossier_id}/action/",
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "review",
                "expected_revision": str(manifest["revision"]),
                "expected_digest": manifest["snapshot_digest"],
                "decision": "approve",
                "note": "R0 HTTP proof holder approve",
            },
            referer_path=review_path,
        )
        lines.append(f"POST holder approve status={status}")
        if status not in {200, 302}:
            raise CommandError("holder_approve_failed")

        review = (
            HumanReview.objects.filter(revision=revision, decision=HumanReview.Decision.APPROVE)
            .order_by("-created_at")
            .first()
        )
        if review is None:
            raise CommandError("holder_review_missing")
        if review.pk in reviews_before_ids:
            raise CommandError("holder_review_not_new_delta")
        holder_user = get_user_model().objects.get(username=manifest["holder_username"])
        if review.reviewer_id != holder_user.pk:
            raise CommandError("holder_reviewer_mismatch")
        if review.reviewed_digest != manifest["snapshot_digest"]:
            raise CommandError("holder_reviewed_digest_mismatch")
        if review.revision_id != revision.pk or revision.revision != manifest["revision"]:
            raise CommandError("holder_revision_mismatch")
        if review.authority_internal_uid != manifest["holder_internal_uid"]:
            raise CommandError("holder_authority_uid_mismatch")
        lines.append(
            "holder_approve_ok "
            f"review_id={review.pk} revision={revision.revision} "
            f"digest={review.reviewed_digest[:16]}… "
            f"authority={review.authority_internal_uid}"
        )

        self.stdout.write("\n".join(lines))
        self.stdout.write("R0_HTTP_PROOF_OK")
