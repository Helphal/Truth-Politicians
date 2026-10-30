"""ORM V3 architecture consumption, SYNTH only; no model or human judgment."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from atelier.models import HumanReview, VerificationDelivery
from atelier.services import proof_architecture as architecture
from atelier.services import verification as verification
from atelier.services import verification_counterevidence as counter
from atelier.services import verification_counterevidence_executor as executor
from atelier.services import verification_plan_executor as shared
from atelier.services.canonical import sha256_digest
from atelier.services.verification_export import build_site_packet
from atelier.tests import test_verification_counterevidence_executor as fixture
from atelier.tests.test_proof_preflight_counterevidence import rights_fixture


class SynthV3:
    SYNTH = True
    identity = {'model': 'SYNTH', 'model_reasoning_effort': 'none', 'fixture': 'SYNTH_V3_architecture_no_provider'}

    def __init__(self, action=None):
        self.action, self.calls, self.raw = action, [], {}

    def run(self, stage, packet, directory, check):
        if stage != executor.STAGE:
            raise AssertionError('SYNTH adversarial stage required')
        check()
        self.calls.append(packet['batch_id'])
        content = packet['adversarial']
        pids = [p['id'] for source in packet['sources'] for p in source['passages']]
        value = {'contract': counter.ADVERSARIAL_RESPONSE_V3, 'fingerprint': packet['fingerprint'],
            'batch_id': packet['batch_id'],
            'claims': {claim['id']: {'conclusion': 'no_objection',
                'reason': 'SYNTH fictional structural fixture only.',
                'support_relations': {str(i): {'support_sha256': content['support_references'][claim['id']][str(i)],
                    'relation': 'supports', 'reason': 'SYNTH reference fixture only.'}
                    for i, support in enumerate(claim['support'])}} for claim in content['claims']},
            'axes': {axis: {'status': 'not_found' if content.get('conclusion_dependencies', {}).get(axis, {}).get('required', True) else 'not_applicable',
                'reason': 'SYNTH fixture, never proof of absence.', 'searched_passage_ids': pids, 'findings': []}
                for axis in counter.AXES},
            'limitations': ['SYNTH no provider, political assessment or human review.']}
        if self.action:
            self.action(value, packet)
        self.raw[packet['batch_id']] = deepcopy(value)
        return value


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CounterV3ArchitectureTests(TestCase):
    setUp = fixture.CounterExecutorTests.setUp
    first = fixture.CounterExecutorTests.first
    allocate = fixture.CounterExecutorTests.allocate

    def receive(self, action=None):
        with patch.object(shared.ProtocolCLI, '__init__', side_effect=AssertionError('provider forbidden')), \
             patch.object(executor.AdversarialCLI, '__init__', side_effect=AssertionError('provider forbidden')), \
             patch.object(executor.CodexCLI, 'run', side_effect=AssertionError('provider forbidden')):
            self.first()
            before = deepcopy(self.first_receipt.attempt.result)
            path, plan = executor.prepare_adversarial_plan(self.revision.pk, user=self.counter_user,
                first_receipt_id=self.first_receipt.pk, model='SYNTH', effort='none',
                response_contract=counter.ADVERSARIAL_RESPONSE_V3)
            allocation = self.allocate(plan, self.counter_user, 'SYNTH:counter-architecture-v3')
            runner = SynthV3(action)
            artifact = executor.execute_adversarial_plan(path, user=self.counter_user,
                allocation_path=allocation, execution_id='SYNTH:counter-architecture-v3', runner=runner)
            received = deepcopy(artifact.payload)
            assessment = architecture.receive_local_assessment(self.revision,
                rights_report=rights_fixture(verification.snapshot(self.revision)))
            report = architecture.assessment_for_revision(self.revision)
            self.assertIsNotNone(report)
            self.assertEqual(report['assessment_digest'], assessment.payload_digest)
            self.assertEqual(report['independent_counterevidence'], received)
            self.assertEqual(received['responses'], [runner.raw[b['batch_id']] for b in plan['batches']])
            for batch in plan['batches']:
                directory = executor._directory(plan) / batch['batch_id'] / '1'
                self.assertEqual(shared._read(directory / 'raw-return.json'), runner.raw[batch['batch_id']])
                self.assertEqual(shared._read(directory / 'validated.json'), runner.raw[batch['batch_id']])
            self.first_receipt.attempt.refresh_from_db()
            self.assertEqual(self.first_receipt.attempt.result, before)
            artifact.refresh_from_db()
            self.assertEqual(artifact.payload, received)
            self.assertEqual(artifact.payload_digest, sha256_digest(received))
            self.assertEqual(HumanReview.objects.count(), 0)
            self.assertEqual(VerificationDelivery.objects.count(), 0)
            return report

    def test_V3_object_claims_axes_consumed_without_raw_rewrite_or_publication(self):
        report = self.receive()
        self.assertEqual(report['gates']['counterevidence_complete']['status'], 'complete')
        self.assertEqual(report['gates']['publishable']['status'], 'blocked')
        self.assertNotEqual(report['gates']['human_reviewed_exact_revision']['status'], 'complete')
        with patch('atelier.services.verification_export._sign_packet') as sign:
            with self.assertRaisesRegex(ValidationError, 'site_export_refused:'):
                build_site_packet(self.revision, user=self.user, environment='test', sequence=1, mapping={})
            sign.assert_not_called()

    def test_V3_qualified_claim_objection_preserves_exact_claim_key(self):
        chosen = []
        def qualify(value, packet):
            cid, judgment = next((cid, j) for cid, j in value['claims'].items() if j['support_relations'])
            chosen.append(cid)
            judgment['conclusion'] = 'qualified'
            judgment['support_relations']['0']['relation'] = 'context_only'
        report = self.receive(qualify)
        projected = [o for o in report['objections'] if o.get('conclusion') == 'qualified']
        self.assertEqual({o['claim_id'] for o in projected}, set(chosen))
        self.assertTrue(all(type(o['support_relations']) is dict for o in projected))
        self.assertEqual(report['gates']['publishable']['status'], 'blocked')

    def test_V3_found_axis_objection_preserves_exact_axis_key(self):
        def found(value, packet):
            claim = next(c for c in packet['adversarial']['claims'] if c['support'])
            support = claim['support'][0]
            value['axes']['quote_out_of_context'].update(status='found', findings=[
                {'claim_id': claim['id'], 'passage_id': support['passage_id'], 'quote': support['quote'],
                 'relation': 'qualifies', 'reason': 'SYNTH authentic fixture quote, fictional objection.'}])
        report = self.receive(found)
        projected = [o for o in report['objections'] if o.get('axis') == 'quote_out_of_context' and o.get('status') == 'found']
        self.assertTrue(projected)
        self.assertTrue(all(o['findings'] for o in projected))
        self.assertEqual(report['gates']['publishable']['status'], 'blocked')
