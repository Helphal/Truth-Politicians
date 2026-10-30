"""Contre-tests indépendants : uniquement base Django de test et fixtures SYNTH."""
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from atelier.models import (
    Source, DocumentVersion, EvidencePassage, UserProfile,
    VerificationJob, VerificationAttempt, VerificationReceipt,
)
from atelier.services import verification as v
from atelier.tests import test_verification as fixture


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class VerificationCounterReviewTests(TestCase):
    setUp = fixture.VerificationTests.setUp
    begin = fixture.VerificationTests.begin
    findings = fixture.VerificationTests.findings
    result = fixture.VerificationTests.result
    complete = fixture.VerificationTests.complete

    def test_two_sources_same_version_require_two_distinct_findings(self):
        source = Source.objects.create(stable_id='SYNTH-SECOND', producer='SYNTH', family='test',
            discovery_url='https://www.senat.fr/test', access_status='active', rights_status='open')
        doc = DocumentVersion.objects.create(source=source, version_id='v1', sha256='b'*64,
            captured_at=timezone.now(), media_type='text/plain', byte_size=20)
        EvidencePassage.objects.create(document=doc, exact_text='Autre source SYNTH.',
            locator='ligne 2', context='Second contexte.', public_url=source.discovery_url)
        self.revision.source_snapshot.append({'source_id':source.stable_id, 'version_id':'v1',
            'sha256':'b'*64, 'public_url':source.discovery_url})
        self.revision.save()
        self.job = v.enqueue(self.revision)
        attempt = self.begin()
        with self.assertRaises(ValidationError):
            v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        findings = self.findings()
        findings['sources'].append({'source_id':source.stable_id, 'version_id':'v1',
            'finding':'Autre preuve SYNTH.', 'locators':['ligne 2'], 'limits':[]})
        self.assertEqual(v.record_evidence(attempt.pk, user=self.user, findings=findings).stage, 'claims')

    def test_source_duplicate_cannot_replace_missing_document(self):
        attempt = self.begin()
        findings = self.findings()
        findings['sources'].append(deepcopy(findings['sources'][0]))
        with self.assertRaises(ValidationError):
            v.record_evidence(attempt.pk, user=self.user, findings=findings)

    def test_evidence_and_finish_ack_loss_return_existing_objects(self):
        attempt = self.begin()
        first = v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        replay = v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        self.assertEqual(first.pk, replay.pk)
        receipt = v.finish(attempt.pk, user=self.user, result=self.result())
        replay = v.finish(attempt.pk, user=self.user, result=self.result())
        self.assertEqual(receipt.pk, replay.pk)
        self.assertEqual(VerificationReceipt.objects.count(), 1)
        self.assertTrue(v.gate(self.revision)[0])

    def test_changed_ack_replay_is_refused(self):
        attempt = self.begin()
        v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        findings = self.findings(); findings['limitations'].append('autre')
        with self.assertRaises(ValidationError):
            v.record_evidence(attempt.pk, user=self.user, findings=findings)
        v.finish(attempt.pk, user=self.user, result=self.result())
        result = self.result(); result['limitations'].append('autre')
        with self.assertRaises(ValidationError):
            v.finish(attempt.pk, user=self.user, result=result)

    def test_whitespace_quote_and_unresolved_action_fail_closed(self):
        attempt = self.begin()
        v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        for quote in (' ', '\t', '\n'):
            with self.subTest(quote=repr(quote)):
                result = self.result(); result['claims'][0]['support'][0]['quote'] = quote
                with self.assertRaises(ValidationError):
                    v.finish(attempt.pk, user=self.user, result=result)
        result = self.result(); result['claims'][0]['action'] = None
        with self.assertRaises(ValidationError):
            v.finish(attempt.pk, user=self.user, result=result)
        self.assertEqual(VerificationReceipt.objects.count(), 0)

    def test_input_snapshot_corruption_after_evidence_blocks_finish(self):
        attempt = self.begin()
        v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        snapshot = deepcopy(self.job.input_snapshot)
        snapshot['content']['title'] = 'Titre corrompu après première passe'
        VerificationJob.objects.filter(pk=self.job.pk).update(input_snapshot=snapshot)
        with self.assertRaises(ValidationError):
            v.finish(attempt.pk, user=self.user, result=self.result())
        self.assertFalse(v.gate(self.revision)[0])

    def test_result_corruption_and_replay_never_qualify(self):
        receipt = self.complete()
        attempt = receipt.attempt
        result = deepcopy(attempt.result); result['claims'][0]['reason'] = 'altération'
        VerificationAttempt.objects.filter(pk=attempt.pk).update(result=result)
        self.assertFalse(v.gate(self.revision)[0])

    def test_receipt_linked_to_another_job_is_refused(self):
        receipt = self.complete()
        other = VerificationJob.objects.create(revision=self.revision, key='b'*64,
            fingerprint='b'*64, policy=v.POLICY, input_snapshot={}, state='completed')
        VerificationAttempt.objects.filter(pk=receipt.attempt_id).update(job=other)
        self.assertFalse(v.gate(self.revision)[0], 'Un reçu ne doit pas emprunter la tentative d’un autre job.')

    def test_account_revocation_between_stages_rechecked_despite_cached_user(self):
        attempt = self.begin()
        v.record_evidence(attempt.pk, user=self.user, findings=self.findings())
        self.assertTrue(self.user.dv_profile.active)
        UserProfile.objects.filter(user=self.user).update(active=False)
        with self.assertRaises(PermissionDenied):
            v.finish(attempt.pk, user=self.user, result=self.result())
        self.assertEqual(VerificationReceipt.objects.count(), 0)

    def test_admin_extra_permission_does_not_grant_verifier_role(self):
        admin = get_user_model().objects.create_user('SYNTH-admin-counterreview')
        UserProfile.objects.create(user=admin, role='admin', permissions_extra=['verification.run'])
        with self.assertRaises(PermissionDenied):
            v.start(self.job.pk, user=admin, execution_id='synthetic:admin', provider='synthetic')

    def test_unsupported_transcription_stays_blocked_on_complete(self):
        self.passage.transcription_status = 'uncertain'; self.passage.save()
        self.job = v.enqueue(self.revision)
        receipt = self.complete()
        self.assertEqual(receipt.decision, 'not_supported')
        self.assertFalse(v.gate(self.revision)[0])

    def test_ui_does_not_render_active_unsafe_source_links(self):
        self.client.force_login(self.editor)
        for uri in ('javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
                    'file:///C:/Windows/win.ini', 'http://127.0.0.1:9999/'):
            with self.subTest(scheme=uri.split(':')[0]):
                self.passage.public_url = uri; self.passage.save()
                self.job = v.enqueue(self.revision)
                response = self.client.get(f'/verification/{self.job.pk}/')
                self.assertEqual(response.status_code, 200)
                unsafe_href = 'href="' + uri.replace('<','&lt;').replace('>','&gt;') + '"'
                self.assertFalse(unsafe_href in response.content.decode(), 'Lien de source actif non autorise : ' + uri.split(':')[0])

    def test_ui_keeps_valid_source_link_and_escapes_source_text(self):
        self.passage.exact_text = '<script>alert(1)</script>'; self.passage.save()
        self.job = v.enqueue(self.revision)
        self.client.force_login(self.editor)
        response = self.client.get(f'/verification/{self.job.pk}/')
        self.assertContains(response, 'href="https://www.assemblee-nationale.fr/test"')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
