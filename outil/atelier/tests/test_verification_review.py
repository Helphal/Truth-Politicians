"""Tiny SYNTH database tests of the real read-only review implementation."""
from types import SimpleNamespace
import tempfile
import json
import hashlib
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from atelier.models import HumanReview, VerificationAttempt, VerificationReceipt
from atelier.services.verification_review import build_review, guide_markdown, historical_review, ARTIFACT_SCHEMA
from atelier.services.verification_review import _context
from atelier.services.verification import structural_metadata
from atelier.tests import test_verification as fixtures


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class VerificationReviewTests(TestCase):
    setUp = fixtures.VerificationTests.setUp

    def attempt(self, claims=None, error=''):
        result = {'claims': claims} if claims is not None else {}
        return SimpleNamespace(pk=1, number=1, result=result, error_code=error)

    def claims(self):
        return [{**c, 'verdict': 'CONTREDIT', 'reason': 'SYNTH contre-preuve', 'action': 'suspendre',
                 'support': [], 'against': [{'passage_id': self.passage.pk, 'quote': 'SYNTH fait limité.'}]} for c in self.job.input_snapshot['claims']]

    def test_complete_negative_is_not_diffusion_or_approval(self):
        review = build_review(self.job, [self.attempt(self.claims())])
        self.assertEqual(review['axes']['execution'], 'Couverture canonique complète')
        self.assertEqual(review['axes']['documentary'], 'Résultat documentaire non favorable')
        self.assertIn('Revue personnelle', review['axes']['diffusion'])
        self.assertEqual(HumanReview.objects.count(), 0)
        self.assertEqual(VerificationReceipt.objects.count(), 0)
        self.assertEqual(VerificationAttempt.objects.count(), 0)

    def test_missing_duplicate_and_foreign_are_visible(self):
        rows = self.claims()
        review = build_review(self.job, [self.attempt([rows[0], rows[0], {'id': {}, 'value': 'SYNTH'}])])
        attempt = review['attempts'][0]
        self.assertEqual(attempt['duplicates'], 1)
        self.assertEqual(attempt['invalid'], 1)
        self.assertGreater(len(attempt['missing']), 0)
        self.assertEqual(review['axes']['execution'], 'Couverture incomplète')

    def test_format_refusal_is_not_silent_missing_success(self):
        review = build_review(self.job, [self.attempt(error='invalid_model_response')])
        self.assertIn('Refus de format', review['axes']['execution'])
        self.assertEqual(review['axes']['documentary'], 'Non qualifié')
        self.assertIn('brut fournisseur refusé', review['attempts'][0]['raw_note'])

    def test_stale_rights_and_context_links_are_distinct(self):
        self.job.state = 'stale'
        sources = self.job.input_snapshot['sources']
        sources[0]['rights'] = 'unknown'
        review = build_review(self.job, [self.attempt(self.claims())])
        self.assertEqual(review['axes']['execution'], 'Vérification périmée')
        self.assertEqual(review['axes']['diffusion'], 'Droits non qualifiés')
        claim = review['attempts'][0]['groups'][0]['claims'][0]
        self.assertEqual(claim['proofs'][0]['passage']['reference']['source_id'], 'SYNTH-SOURCE')
        self.assertEqual(claim['proofs'][0]['anchor'], f'passage-{self.passage.pk}')

    def test_private_page_requires_authentication_and_does_not_approve(self):
        url = reverse('verification_detail', args=[self.job.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Trois axes distincts')
        self.assertEqual(HumanReview.objects.count(), 0)

    @override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID='SYNTH-different-owner')
    def test_verifier_cannot_create_personal_approval(self):
        from atelier.services.publication import record_human_review
        with self.assertRaises(PermissionDenied):
            record_human_review(dossier_id=self.dossier.pk, expected_revision=1,
                                expected_digest=self.revision.snapshot_digest, user=self.user,
                                decision=HumanReview.Decision.APPROVE, environment='test')
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_guide_version_is_explicit_and_unsafe_link_rejected(self):
        row = {'job_id': 1, 'revision': 1, 'revision_pk': 1, 'title': '<script>SYNTH</script>',
               'short_answer': 'SYNTH', 'main_caveat': 'Réserve SYNTH', 'attribution': 'SYNTH auteur',
               'date': '2026-10-03', 'normalized_locator': 'ligne 1',
               'source_version': {'source_id': 'SYNTH-SOURCE', 'version_id': 'v1', 'sha256': 'a'*64,
                                  'public_url': 'https://www.senat.fr/synth'}}
        guide = guide_markdown({'revisions': [row]}, preview_links={1: '/private/synth.html'})
        self.assertIn('Version de source : v1', guide)
        self.assertIn('Repère : ligne 1', guide)
        self.assertNotIn('[object Object]', guide)
        self.assertNotIn('<script>', guide)
        row['source_version']['public_url'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            guide_markdown({'revisions': [row]}, preview_links={1: '/private/synth.html'})
        row['source_version']['public_url'] = 'https://www.senat.fr/synth'
        for link in ('javascript:alert(1)', 'data:text/html,x', '/private/../secret', '/private/synth>\n[bad](x)'):
            with self.assertRaises(ValueError):
                guide_markdown({'revisions': [row]}, preview_links={1: link})

    def historical_fixture(self, directory):
        from atelier.services.verification import start
        attempt = start(self.job.pk, user=self.user, execution_id='synthetic:review-history', provider='synthetic')
        folder = Path(directory) / f'job-{self.job.pk}'
        folder.mkdir()
        group = {'claim_indices': [0], 'verdict': 'CONTREDIT', 'reason': '<script>SYNTH</script>'}
        raw = json.dumps({'fingerprint': self.job.fingerprint, 'groups': [group]}).encode()
        (folder / 'response.json').write_bytes(raw)
        semantic = [c for c in self.job.input_snapshot['claims'] if not structural_metadata(c, self.job.input_snapshot)]
        claim = {**semantic[0], 'claim_index': 0, 'context': json.loads(_context(self.job.input_snapshot, semantic[0]['location']))}
        artifact = {'contract': ARTIFACT_SCHEMA, 'job_id': self.job.pk, 'revision_pk': self.revision.pk,
                    'fingerprint': self.job.fingerprint, 'attempt_id': attempt.pk, 'raw_file': 'response.json',
                    'raw_sha256': hashlib.sha256(raw).hexdigest(), 'expected_count': len(semantic), 'present_count': 1,
                    'missing_count': len(semantic)-1, 'objections': [{'raw_group_index': 0, 'model_group': group, 'claims': [claim]}]}
        (folder / 'review.json').write_text(json.dumps(artifact), encoding='utf-8')
        return folder, artifact

    def test_historical_refusal_visible_but_never_qualifies(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(DV_VERIFICATION_REVIEW_ARTIFACT_ROOT=directory):
            folder, baseline = self.historical_fixture(directory)
            review = historical_review(self.job)
            self.assertTrue(review['available'])
            self.assertEqual(review['missing_count'], baseline['expected_count'] - 1)
            self.assertIn('aucun reçu nouveau', review['provenance'])
            self.client.force_login(self.user)
            response = self.client.get(reverse('verification_detail', args=[self.job.pk]))
            self.assertContains(response, 'Réponse brute complète refusée')
            self.assertContains(response, '&lt;script&gt;SYNTH&lt;/script&gt;')
            self.assertNotContains(response, '<script>SYNTH</script>')
            self.assertEqual(VerificationReceipt.objects.count(), 0)

    def test_historical_mismatch_path_size_and_silent_group_loss_rejected(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(DV_VERIFICATION_REVIEW_ARTIFACT_ROOT=directory):
            folder, baseline = self.historical_fixture(directory)
            mutations = [{'revision_pk': 999}, {'attempt_id': 999}, {'fingerprint': 'b'*64}, {'raw_file': '../response.json'},
                         {'raw_sha256': 'c'*64}, {'present_count': 2, 'missing_count': 1}, {'objections': []}]
            for change in mutations:
                (folder / 'review.json').write_text(json.dumps({**baseline, **change}), encoding='utf-8')
                self.assertFalse(historical_review(self.job)['available'], change)
            (folder / 'review.json').write_text(json.dumps(baseline), encoding='utf-8')
            with patch('atelier.services.verification_review.MAX_REVIEW_ARTIFACT_BYTES', 10):
                self.assertFalse(historical_review(self.job)['available'])

    def test_historical_expected_and_claim_context_cannot_be_forged(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as directory, override_settings(DV_VERIFICATION_REVIEW_ARTIFACT_ROOT=directory):
            folder, baseline = self.historical_fixture(directory)
            altered = deepcopy(baseline)
            altered['expected_count'] += 1
            altered['missing_count'] += 1
            cases = [altered]
            for key, value in [('id', 'foreign'), ('value', 'SYNTH changed'), ('context', {'forged': True})]:
                altered = deepcopy(baseline)
                altered['objections'][0]['claims'][0][key] = value
                cases.append(altered)
            for altered in cases:
                (folder / 'review.json').write_text(json.dumps(altered), encoding='utf-8')
                self.assertFalse(historical_review(self.job)['available'])
