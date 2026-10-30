"""DV-RACC-001 R1-A — preuves GET/POST/service revue finale (TEST, données synthétiques)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import EditorialDossier, Environment, HumanReview, UserProfile
from atelier.services.accounts import create_account, public_attribution_for, regenerate_login_pseudonym
from atelier.services.publication import evaluate_revision, record_human_review
from atelier.tests.support import AtelierTestCase

PRIVATE = {"HTTP_X_DV_PRIVATE_ENTRY": "1", "HTTP_X_FORWARDED_PROTO": "https"}


@override_settings(
    DV_NETWORK_MODE="disabled",
    DV_PRIVATE_ENTRY_MODE="header",
    DV_TRUST_PRIVATE_ENTRY_PROXY=True,
    DV_PRIVATE_ENTRY_HEADER_META="HTTP_X_DV_PRIVATE_ENTRY",
    DV_PRIVATE_ENTRY_EXPECTED_VALUE="1",
)
class Racc001ReviewAuthorityHttpTests(AtelierTestCase):
    def _holder_admin(self):
        cred = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        user = get_user_model().objects.get(pk=cred.user_id)
        user.set_password("Holder-Proof-99!")
        user.dv_profile.must_change_password = False
        user.dv_profile.save()
        user.save()
        return user, str(user.dv_profile.internal_uid)

    def _editor(self, actor):
        cred = create_account(role=UserProfile.Role.EDITOR, actor=actor, is_test_identity=True)
        user = get_user_model().objects.get(pk=cred.user_id)
        user.set_password("Editor-Proof-99!")
        user.dv_profile.must_change_password = False
        user.dv_profile.save()
        user.save()
        return user

    def test_holder_approve_service_and_eligibility(self):
        holder, uid = self._holder_admin()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            review = record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=holder,
                decision=HumanReview.Decision.APPROVE,
                environment=Environment.TEST,
            )
            self.assertEqual(review.decision, HumanReview.Decision.APPROVE)
            elig = evaluate_revision(revision, Environment.TEST)
            self.assertTrue(elig.allowed)

    def test_holder_post_approve_success(self):
        holder, uid = self._holder_admin()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        client = Client()
        client.force_login(holder)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            resp = client.post(
                reverse("review_action", kwargs={"pk": dossier.pk}),
                {
                    "mode": "review",
                    "expected_revision": revision.revision,
                    "expected_digest": revision.snapshot_digest,
                    "decision": "approve",
                    "note": "preuve R1-A",
                },
                **PRIVATE,
            )
        self.assertIn(resp.status_code, {200, 302})
        self.assertTrue(
            HumanReview.objects.filter(
                revision=revision, decision=HumanReview.Decision.APPROVE
            ).exists()
        )

    def test_editor_post_approve_forbidden_no_state_change(self):
        holder, uid = self._holder_admin()
        editor = self._editor(holder)
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        dossier.refresh_from_db()
        state_before = dossier.state
        reviews_before = HumanReview.objects.filter(revision=revision).count()
        client = Client()
        client.force_login(editor)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            url = reverse("review_action", kwargs={"pk": dossier.pk})
            resp = client.post(
                url,
                {
                    "mode": "review",
                    "expected_revision": revision.revision,
                    "expected_digest": revision.snapshot_digest,
                    "decision": "approve",
                    "note": "",
                },
                **PRIVATE,
            )
        dossier.refresh_from_db()
        revision.refresh_from_db()
        self.assertIn(resp.status_code, {403, 302})
        self.assertEqual(dossier.state, state_before)
        self.assertEqual(HumanReview.objects.filter(revision=revision).count(), reviews_before)
        self.assertFalse(
            HumanReview.objects.filter(
                revision=revision, decision=HumanReview.Decision.APPROVE
            ).exists()
        )

    def test_suspended_account_cannot_review(self):
        holder, uid = self._holder_admin()
        holder.dv_profile.account_status = UserProfile.AccountStatus.SUSPENDED
        holder.dv_profile.save()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            client = Client()
            client.force_login(holder)
            resp = client.get(reverse("review", kwargs={"pk": dossier.pk}), **PRIVATE)
        self.assertEqual(resp.status_code, 403)

    def test_must_change_password_redirects_before_review(self):
        holder, uid = self._holder_admin()
        holder.dv_profile.must_change_password = True
        holder.dv_profile.save()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        client = Client()
        client.force_login(holder)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            resp = client.get(reverse("review", kwargs={"pk": dossier.pk}), **PRIVATE)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("mot-de-passe", resp.url)

    def test_stale_revision_post_returns_conflict(self):
        holder, uid = self._holder_admin()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        client = Client()
        client.force_login(holder)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            resp = client.post(
                reverse("review_action", kwargs={"pk": dossier.pk}),
                {
                    "mode": "review",
                    "expected_revision": revision.revision,
                    "expected_digest": "0" * 64,
                    "decision": "approve",
                    "note": "",
                },
                **PRIVATE,
            )
        self.assertEqual(resp.status_code, 409)

    def test_attribution_stable_after_pseudonym_rotation(self):
        holder, uid = self._holder_admin()
        editor = self._editor(holder)
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        uid_before = editor.dv_profile.internal_uid
        label_before = public_attribution_for(editor)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            review = record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=editor,
                decision=HumanReview.Decision.REQUEST_CHANGES,
                environment=Environment.TEST,
                note="trace attribution",
            )
        regenerate_login_pseudonym(target_user=editor, actor=holder)
        editor.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(editor.dv_profile.internal_uid, uid_before)
        self.assertEqual(review.reviewer_id, editor.pk)
        self.assertNotEqual(public_attribution_for(editor), label_before)

    def test_detached_review_get_holder_ok_editor_forbidden(self):
        holder, uid = self._holder_admin()
        editor = self._editor(holder)
        dossier, revision, _ = self.make_revision(
            EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST, data_scope="synthetic"
        )
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            ch = Client()
            ch.force_login(holder)
            ok = ch.get(reverse("detached_review", kwargs={"pk": dossier.pk}), **PRIVATE)
            self.assertEqual(ok.status_code, 200)
            ce = Client()
            ce.force_login(editor)
            denied = ce.get(reverse("detached_review", kwargs={"pk": dossier.pk}), **PRIVATE)
            self.assertEqual(denied.status_code, 403)
