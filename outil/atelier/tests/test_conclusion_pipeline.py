"""Received dependency pipeline on SYNTH originals, without provider or human."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from atelier.models import HumanReview, ProofArtifact, UserProfile, VerificationDelivery
from atelier.services import verification as v, verification_plan_executor as shared
from atelier.services import proof_architecture as architecture
from atelier.services import verification_counterevidence as counter
from atelier.services import verification_counterevidence_executor as executor
from atelier.services import conclusion_dependencies as dependencies
from atelier.services import rights_qualification as rights
from atelier.services.canonical import sha256_digest
from atelier.services.proof_classification import classify_snapshot
from atelier.services.provenance import build_provenance_report
from atelier.services.verification_preflight import build_preflight
from atelier.tests import test_proof_architecture as fixture
from atelier.tests.test_verification_plan_executor import SynthRunner
from atelier.tests.test_verification_counterevidence_executor import AdversarialSynth


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConclusionPipelineTests(TestCase):
    def setUp(self):
        fixture.ProofArchitectureTests.setUp(self)
        url = 'https://www.assemblee-nationale.fr/dyn/16/scrutins/3289'
        self.source.document_url = url
        self.source.save(update_fields=['document_url'])
        ref = {**self.revision.source_snapshot[0], 'public_url': url}
        self.revision.source_snapshot = [ref]
        payload = deepcopy(self.revision.public_payload)
        def references(value):
            if isinstance(value, dict):
                if 'source_version' in value:
                    value['source_version'] = deepcopy(ref)
                for child in value.values():
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)
        references(payload)
        self.revision.public_payload = payload
        self.revision.snapshot_digest = sha256_digest(payload)
        self.revision.save()
        self.revision.conclusion_contract = dependencies.build_conclusion(
            v.snapshot(self.revision), archetype=dependencies.ARCHETYPES[0])
        self.revision.save(update_fields=['conclusion_contract'])
        self.job = v.enqueue(self.revision)
        # This approval hash is scoped to a declared SYNTH fixture only. No
        # actual rights or editorial opinion is produced by these tests.
        raw = b'SYNTH conditions fixture. No real rights qualification.'
        digest = hashlib.sha256(raw).hexdigest()
        self.conditions = self.root / 'conditions'
        self.conditions.mkdir()
        (self.conditions / (digest + '.html')).write_bytes(raw)
        conditions_url = 'https://www.assemblee-nationale.fr/dyn/info-site'
        (self.conditions / 'RECU-COLLECTE-CONDITIONS.json').write_text(json.dumps({
            'contract': 'dv.primary-conditions-collection/1', 'credentials_used': 0, 'cookies_saved': 0,
            'rows': [{'key': 'an_text_conditions', 'requested_url': conditions_url,
                'url': conditions_url, 'captured_at': timezone.now().isoformat(),
                'sha256': digest, 'filename': digest + '.html', 'byte_size': len(raw),
                'media_type': 'text/html', 'http_status': 200, 'status': 'received'}]}), encoding='utf8')
        approved = dict(rights._APPROVED_CONDITIONS)
        approved['an_text_conditions'] = (conditions_url, digest)
        guard = patch.object(rights, '_APPROVED_CONDITIONS', approved)
        guard.start(); self.addCleanup(guard.stop)
        setting = override_settings(DV_RIGHTS_EVIDENCE_DIR=self.conditions)
        setting.enable(); self.addCleanup(setting.disable)

    def prepare(self):
        value = v.snapshot(self.revision)
        qualification = rights.build_source_qualification(value, self.revision.source_snapshot[0])
        report = rights.build_rights_report(value, [qualification])
        classification = classify_snapshot(value, provenance_report=build_provenance_report(value))
        preflight = build_preflight(value, classification, rights_report=report)
        self.assertEqual(preflight['status'], 'ready', preflight['checks'])
        self.classification, self.rights, self.preflight = classification, report, preflight
        path, plan = shared.prepare_plan(self.job.pk, user=self.user, model='SYNTH', effort='none',
            classification=classification, preflight=preflight)
        return path, plan

    def allocate(self, plan, actor, label):
        value = {'contract': shared.ALLOCATION, 'allocation_id': label,
            'authorization': 'explicit_dv_verite', 'authority_reference': 'SYNTH fixture only',
            'owner_id': actor.pk, 'plan_digests': [plan['plan_digest']],
            'fingerprints': [plan['fingerprint']], 'job_ids': [plan['job_id']],
            'model': 'SYNTH', 'effort': 'none',
            'max_phases': plan['allocation_proposal']['proposed_ceiling'],
            'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()}
        path = self.root / 'plans' / (label.replace(':', '-') + '.json')
        if not path.exists():
            shared._write(path, value)
        return path

    def first(self):
        path, plan = self.prepare()
        runner = SynthRunner(plan)
        receipt = shared.execute_plan(path, user=self.user,
            allocation_path=self.allocate(plan, self.user, 'SYNTH:first-v2'),
            execution_id='SYNTH:first-v2', runner=runner)
        return path, plan, receipt, runner

    def test_graph_and_current_rights_received_immutably_and_recomputed_on_read(self):
        _, plan = self.prepare()
        self.assertEqual(plan['proof_architecture']['contract'], 'dv.layered-plan/2')
        self.assertEqual(ProofArtifact.objects.filter(kind='dependency_graph').count(), 1)
        self.assertEqual(ProofArtifact.objects.filter(kind='rights').count(), 1)
        self.assertEqual(architecture.received_rights_for_render(self.revision, 'private_review'), self.rights)
        artifact = architecture.receive_local_assessment(self.revision, rights_report=self.rights)
        self.assertEqual(artifact.payload['schema'], 'dv.proof-architecture/2')
        self.assertIsNotNone(architecture.assessment_for_revision(self.revision))
        with self.assertRaisesRegex(ValidationError, 'immutable'):
            ProofArtifact.objects.filter(kind='dependency_graph').update(payload={})
        self.source.document_url = 'https://www.assemblee-nationale.fr/dyn/16/scrutins/908'
        self.source.save(update_fields=['document_url'])
        self.assertIsNone(architecture.received_rights_for_render(self.revision, 'private_review'))
        self.assertIsNone(architecture.assessment_for_revision(self.revision))

    def test_unreceived_graph_refuses_before_allocation_or_cli(self):
        path, plan = self.prepare()
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM atelier_proofartifact WHERE kind = %s', ['dependency_graph'])
        with patch.object(shared, '_allocation') as allocate, patch.object(shared, 'ProtocolCLI') as factory:
            with self.assertRaisesRegex(ValidationError, 'dependency_graph_not_received'):
                shared.execute_plan(path, user=self.user, allocation_path=None,
                    execution_id='SYNTH:missing-graph', runner=SynthRunner(plan))
            allocate.assert_not_called(); factory.assert_not_called()
        self.assertEqual(self.job.attempts.count(), 0)

    def test_forged_usage_does_not_receive_rights_or_graph(self):
        self.prepare()
        bad = deepcopy(self.preflight)
        bad['rights_report']['usages'][0]['status'] = 'unqualified'
        with self.assertRaises(ValidationError):
            architecture.receive_preflight(self.revision, self.classification, bad)
        self.assertEqual(ProofArtifact.objects.filter(kind='rights').count(), 1)

    def test_exact_conclusion_binds_fingerprint_and_source_changes_stale_receipts(self):
        self.prepare()
        before = sha256_digest(v.snapshot(self.revision))
        self.revision.main_caveat = 'SYNTH nouvelle réserve de période.'
        self.revision.save(update_fields=['main_caveat'])
        self.assertNotEqual(before, sha256_digest(v.snapshot(self.revision)))
        with self.assertRaises(ValidationError):
            classify_snapshot(v.snapshot(self.revision))
        self.assertIsNone(architecture.received_rights_for_render(self.revision, 'private_review'))

    def test_private_dependency_view_is_read_only_and_preserves_nonrequired_unknowns(self):
        self.prepare()
        architecture.receive_local_assessment(self.revision, rights_report=self.rights)
        self.client.force_login(self.user)
        before = ProofArtifact.objects.count()
        response = self.client.get(reverse('verification_detail', args=[self.job.pk]), {'problem': 'dependencies'})
        self.assertContains(response, 'Preuves nécessaires')
        self.assertContains(response, 'Déclaration personnelle et vote personnel')
        self.assertContains(response, 'Non requis pour cette conclusion')
        self.assertNotContains(response, '[object Object]')
        self.assertEqual(ProofArtifact.objects.count(), before)
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_layered_v2_replay_and_independent_counter_axes_never_create_human_or_delivery(self):
        path, plan, receipt, runner = self.first()
        self.assertEqual(receipt.attempt.result['contract'], 'dv.layered-result/2')
        self.assertTrue(any(c['status'] == 'not_applicable' for c in receipt.attempt.result['claims']))
        replay = SynthRunner(plan)
        again = shared.execute_plan(path, user=self.user,
            allocation_path=self.allocate(plan, self.user, 'SYNTH:first-v2'),
            execution_id='SYNTH:first-v2', runner=replay)
        self.assertEqual(again.pk, receipt.pk); self.assertEqual(replay.calls, [])
        actor = get_user_model().objects.create_user('SYNTH:counter-v2')
        UserProfile.objects.create(user=actor, role='verifier', is_test_identity=True)
        counter_path, counter_plan = executor.prepare_adversarial_plan(self.revision.pk,
            user=actor, first_receipt_id=receipt.pk, model='SYNTH', effort='none')
        def version2(raw, packet, directory):
            raw['contract'] = counter.ADVERSARIAL_RESPONSE_V2
            required = packet['adversarial']['conclusion_dependencies']
            for axis in raw['axes']:
                if not required[axis['axis']]['required']:
                    axis['status'] = 'not_applicable'
        adversarial = AdversarialSynth(action=version2)
        artifact = executor.execute_adversarial_plan(counter_path, user=actor,
            allocation_path=self.allocate(counter_plan, actor, 'SYNTH:counter-v2'),
            execution_id='SYNTH:counter-v2', runner=adversarial)
        self.assertEqual(artifact.schema, 'dv.counterevidence/2')
        self.assertTrue(artifact.payload['runtime_identity_verified'])
        self.assertTrue(any(a['status'] == 'not_applicable'
            for response in artifact.payload['responses'] for a in response['axes']))
        assessment = architecture.receive_local_assessment(self.revision, rights_report=self.rights).payload
        self.assertEqual(assessment['gates']['semantic_verification_complete']['status'], 'complete')
        self.assertEqual(assessment['gates']['counterevidence_complete']['status'], 'complete')
        self.assertNotEqual(assessment['gates']['publishable']['status'], 'complete')
        self.assertEqual(HumanReview.objects.count(), 0); self.assertEqual(VerificationDelivery.objects.count(), 0)
