"""Independent B005 counter-review; all data are SYNTH and owned by TestCase."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from atelier.models import HumanReview, UserProfile, VerificationAttempt, VerificationReceipt
from atelier.services import verification as v
from atelier.services.canonical import sha256_digest
from atelier.tests import test_verification as fixture


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ActionIndependentCounterreview(TestCase):
    setUp = fixture.VerificationTests.setUp
    begin = fixture.VerificationTests.begin
    findings = fixture.VerificationTests.findings
    result = fixture.VerificationTests.result
    complete = fixture.VerificationTests.complete

    def legacy(self):
        with patch.object(v, 'POLICY', v.LEGACY_POLICY):
            self.job = v.enqueue(self.revision)
            return self.complete()

    def test_metadata_required_action_is_not_exempted_by_non_factual_verdict(self):
        result = self.result()
        claim = next(c for c in result['claims'] if c['location'] == '/source_snapshot/0/source_id')
        claim.update(nature='metadata', verdict='NON_VERIFIABLE_COMME_FAIT', support=[])
        self.assertTrue(v._check_result(self.job, result))
        for action in ['corriger', 'suspendre', 'rechercher', 'attribuer', 'nuancer']:
            with self.subTest(action=action):
                claim['action'] = action
                self.assertFalse(v._check_result(self.job, result))

    def test_resolution_cannot_clear_against_or_non_supported_verdict(self):
        result = self.result(); claim = next(c for c in result['claims'] if c['location'] == '/title')
        claim['action'] = 'nuancer'
        claim['action_resolution'] = {'status': 'already_present', 'location': '/title',
                                      'quote': 'fait limité', 'reason': 'La limite existe dans ce titre SYNTH.'}
        self.assertTrue(v._check_result(self.job, result))
        claim['against'] = deepcopy(claim['support'])
        self.assertFalse(v._check_result(self.job, result))
        claim['against'] = []
        for verdict in v.VERDICTS - {'ETAYE_DANS_PERIMETRE'}:
            with self.subTest(verdict=verdict):
                claim['verdict'] = verdict
                self.assertFalse(v._check_result(self.job, result))

    def test_late_required_action_result_cannot_finish_after_lease_expiry(self):
        a = self.begin(); v.record_evidence(a.pk, user=self.user, findings=self.findings())
        VerificationAttempt.objects.filter(pk=a.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        bad = self.result(); bad['claims'][0]['action'] = 'corriger'
        with self.assertRaises(ValidationError): v.finish(a.pk, user=self.user, result=bad)
        self.assertFalse(VerificationReceipt.objects.exists())
        self.assertFalse(v.gate(self.revision)[0])

    def test_legacy_revalidation_does_not_resurrect_invalidated_a_b_a_generation(self):
        old = self.legacy()
        with patch.object(v, 'POLICY', v.LEGACY_POLICY):
            self.passage.context = 'SYNTH nouvelle contre-preuve constatée.'; self.passage.save()
            v.enqueue(self.revision)
            self.passage.context = 'Source fictive réservée au banc.'; self.passage.save()
            newest = v.enqueue(self.revision)
        self.assertNotEqual(newest.pk, old.job_id)
        self.assertEqual(newest.fingerprint, old.fingerprint)
        with self.assertRaises(ValidationError): v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertFalse(v.gate(self.revision)[0])

    def test_legacy_corrupt_result_digest_cannot_revalidate(self):
        old = self.legacy()
        VerificationAttempt.objects.filter(pk=old.attempt_id).update(result_digest='0'*64)
        with self.assertRaises(ValidationError): v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertFalse(v.gate(self.revision)[0])

    def test_legacy_does_not_resurrect_after_current_policy_a_b_a_generation(self):
        old = self.legacy()
        self.passage.context = 'SYNTH nouvelle observation sous politique 1.1.'; self.passage.save()
        changed = v.enqueue(self.revision)
        self.assertEqual(changed.policy, v.POLICY)
        self.passage.context = 'Source fictive réservée au banc.'; self.passage.save()
        returned = v.enqueue(self.revision)
        self.assertNotEqual(changed.pk, returned.pk)
        with self.assertRaisesMessage(ValidationError, 'legacy_generation_superseded'):
            v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertFalse(v.gate(self.revision)[0])

    def test_policy_only_generation_still_allows_traced_compatibility(self):
        old = self.legacy()
        current = v.enqueue(self.revision)
        self.assertNotEqual(current.pk, old.job_id)
        compatible = deepcopy(old.job.input_snapshot); compatible['policy'] = v.POLICY
        self.assertEqual(compatible, current.input_snapshot)
        receipt = v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertTrue(v.gate(self.revision)[0])
        self.assertEqual(receipt.job_id, current.pk)
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_current_negative_action_receipt_cannot_be_overruled_by_legacy(self):
        old = self.legacy(); self.job = v.enqueue(self.revision)
        attempt = self.begin(); v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        result = self.result(); result['claims'][0]['action'] = 'rechercher'
        negative = v.finish(attempt.pk, user=self.user, result=result)
        self.assertEqual(negative.decision, 'not_supported')
        with self.assertRaises(ValidationError): v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertFalse(v.gate(self.revision)[0])

    def test_current_attempt_digest_corruption_closes_gate(self):
        receipt = self.complete()
        VerificationAttempt.objects.filter(pk=receipt.attempt_id).update(result_digest='0'*64)
        self.assertFalse(v.gate(self.revision)[0])

    def test_idempotent_finish_rejects_wrong_receipt_fingerprint(self):
        receipt = self.complete(); result = deepcopy(receipt.attempt.result)
        VerificationReceipt.objects.filter(pk=receipt.pk).update(fingerprint='0'*64)
        self.assertFalse(v.gate(self.revision)[0])
        with self.assertRaises(ValidationError): v.finish(receipt.attempt_id, user=self.user, result=result)

    def test_legacy_corrupt_claim_keeps_transaction_closed(self):
        old = self.legacy(); bad = deepcopy(old.attempt.result)
        bad['claims'][0]['value'] = 'SYNTH assertion falsifiée'
        digest = sha256_digest(bad)
        VerificationAttempt.objects.filter(pk=old.attempt_id).update(result=bad, result_digest=digest)
        VerificationReceipt.objects.filter(pk=old.pk).update(result_digest=digest)
        count = VerificationReceipt.objects.count()
        with self.assertRaises(ValidationError): v.revalidate_legacy_receipt(old.pk, user=self.user)
        self.assertEqual(VerificationReceipt.objects.count(), count)
        self.assertFalse(v.gate(self.revision)[0])

    def test_revoked_original_verifier_cannot_be_laundered_by_active_verifier(self):
        old = self.legacy()
        UserProfile.objects.filter(user=self.user).update(active=False)
        active = get_user_model().objects.create_user('SYNTH-active-second-verifier')
        UserProfile.objects.create(user=active, role='verifier', is_test_identity=True)
        with self.assertRaises(PermissionDenied): v.revalidate_legacy_receipt(old.pk, user=active)
        self.assertEqual(HumanReview.objects.count(), 0)
