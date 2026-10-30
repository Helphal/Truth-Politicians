"""RACC-001 — suspension du titulaire invalide une revue approuvée."""
from django.core.exceptions import ValidationError

from atelier.models import EditorialDossier, Environment, UserProfile
from atelier.services.publication import authorize_and_queue, evaluate_revision, packet_for_event
from atelier.tests.support import AtelierTestCase


class ReviewerSuspensionTests(AtelierTestCase):
    def test_suspended_holder_revokes_eligibility_and_export(self):
        reviewer = self.make_user()
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, environment=Environment.TEST)
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, Environment.TEST)
        reviewer.dv_profile.account_status = UserProfile.AccountStatus.SUSPENDED
        reviewer.dv_profile.save()
        elig = evaluate_revision(revision, Environment.TEST)
        self.assertFalse(elig.allowed)
        self.assertEqual(elig.reason, "review_missing_or_stale")
        with self.assertRaises(ValidationError):
            packet_for_event(event)
