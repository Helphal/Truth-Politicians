"""DV-RACC-001 R1-A — autorité de revue finale liée à internal_uid."""
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings

from atelier.models import EditorialDossier, Environment, HumanReview, UserProfile
from atelier.services.publication import evaluate_revision, record_human_review
from atelier.services.review_authority import has_final_discourse_review_authority
from atelier.tests.support import AtelierTestCase


class FinalReviewAuthorityTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.admin_uid = str(uuid.uuid4())
        self.editor_uid = str(uuid.uuid4())
        self.admin = User.objects.create_user(username="admin-test", password="ComplexPass-1!")
        self.editor = User.objects.create_user(username="editor-test", password="ComplexPass-2!")
        self.admin_profile = UserProfile.objects.create(
            user=self.admin,
            role=UserProfile.Role.ADMIN,
            login_pseudonym="Admin-Test",
            internal_uid=self.admin_uid,
            is_test_identity=True,
            active=True,
        )
        self.editor_profile = UserProfile.objects.create(
            user=self.editor,
            role=UserProfile.Role.EDITOR,
            login_pseudonym="Editor-Test",
            internal_uid=self.editor_uid,
            is_test_identity=True,
            active=True,
        )

    @override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID="")
    def test_default_deny_without_configured_authority(self):
        self.assertFalse(has_final_discourse_review_authority(self.admin_profile, Environment.TEST))

    def test_only_configured_uid_has_final_authority(self):
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=self.admin_uid):
            self.assertTrue(has_final_discourse_review_authority(self.admin_profile, Environment.TEST))
            self.assertFalse(has_final_discourse_review_authority(self.editor_profile, Environment.TEST))

    def test_editor_cannot_record_approve_even_with_old_reviewer_imply(self):
        from atelier.models import EditorialDossier, EditorialRevision

        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=self.admin_uid):
            dossier = EditorialDossier.objects.create(
                stable_id="TEST-RACC-001",
                content_kind=EditorialDossier.Kind.SPEECH_VOTE,
                state=EditorialDossier.State.DRAFT,
                data_scope="synthetic",
                current_revision=1,
            )
            revision = EditorialRevision.objects.create(
                dossier=dossier,
                revision=1,
                title="t",
                short_answer="a",
                main_caveat="c",
                reasoning="r",
                public_payload={"x": 1},
                snapshot_digest="d" * 64,
                source_snapshot={},
            )
            with self.assertRaises(PermissionDenied):
                record_human_review(
                    dossier_id=dossier.pk,
                    expected_revision=1,
                    expected_digest=revision.snapshot_digest,
                    user=self.editor,
                    decision=HumanReview.Decision.APPROVE,
                    environment=Environment.TEST,
                )

    def test_holder_approve_success(self):
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=self.admin_uid):
            dossier, revision, _ = self.make_revision(
                EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST
            )
            record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=self.admin,
                decision=HumanReview.Decision.APPROVE,
                environment=Environment.TEST,
            )
            revision.refresh_from_db()
            self.assertTrue(evaluate_revision(revision, Environment.TEST).allowed)

    def test_authority_reaches_decision_validation_not_role_wall(self):
        from atelier.models import EditorialDossier, EditorialRevision

        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=self.admin_uid):
            dossier = EditorialDossier.objects.create(
                stable_id="TEST-RACC-002",
                content_kind=EditorialDossier.Kind.SPEECH_VOTE,
                state=EditorialDossier.State.DRAFT,
                data_scope="synthetic",
                current_revision=1,
            )
            EditorialRevision.objects.create(
                dossier=dossier,
                revision=1,
                title="t",
                short_answer="a",
                main_caveat="c",
                reasoning="r",
                public_payload={"x": 1},
                snapshot_digest="d" * 64,
                source_snapshot={},
            )
            with self.assertRaises(ValidationError):
                record_human_review(
                    dossier_id=dossier.pk,
                    expected_revision=1,
                    expected_digest="d" * 64,
                    user=self.admin,
                    decision="SENTINEL_INVALID_DECISION",
                    environment=Environment.TEST,
                )
