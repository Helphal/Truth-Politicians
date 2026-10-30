"""Qualified SYNTH adversarial pipeline; never a political or provider proof."""
from copy import deepcopy
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from atelier.models import HumanReview, UserProfile, VerificationAttempt, VerificationReceipt, ProofArtifact, VerificationDelivery
from atelier.services import proof_architecture as architecture
from atelier.services import verification_counterevidence as counter
from atelier.services import verification_counterevidence_executor as executor
from atelier.services import verification_plan_executor as shared
from atelier.services.canonical import sha256_digest
from atelier.services.proof_classification import classify_snapshot
from atelier.services.provenance import build_provenance_report
from atelier.services.verification_preflight import build_preflight, estimate_cost
from atelier.services.verification_executor import ExecutionFailure
from atelier.tests import test_proof_architecture as fixture
from atelier.tests.test_verification_plan_executor import SynthRunner
from atelier.tests.test_proof_preflight_counterevidence import rights_fixture


def raw_response(packet):
    content = packet['adversarial']
    passages = [p['id'] for s in packet['sources'] for p in s['passages']]
    return {'contract': counter.ADVERSARIAL_RESPONSE, 'fingerprint': packet['fingerprint'],
        'batch_id': packet['batch_id'],
        'claims': [{'claim_id': c['id'], 'conclusion': 'no_objection', 'reason': 'SYNTH schema fixture only.',
            'support_relations': [{'support_index': i, 'relation': 'supports', 'reason': 'SYNTH transport binding only.',
                                  'passage_id': support['passage_id'], 'quote': support['quote']}
                                 for i, support in enumerate(c['support'])]} for c in content['claims']],
        'axes': [{'axis': axis, 'status': 'not_found', 'reason': 'SYNTH fixture; never proof of absence.',
                  'searched_passage_ids': passages, 'findings': []} for axis in counter.AXES],
        'limitations': ['SYNTH controlled fixture; no real political assessment or provider call.']}


class AdversarialSynth:
    SYNTH = True
    identity = {'model': 'SYNTH', 'model_reasoning_effort': 'none', 'fixture': 'SYNTH_adversarial_no_provider'}

    def __init__(self, action=None):
        self.calls = []
        self.action = action

    def run(self, stage, packet, directory, check):
        if stage != executor.STAGE:
            raise AssertionError('SYNTH wrong adversarial stage')
        self.calls.append(packet['batch_id'])
        check()
        value = raw_response(packet)
        if self.action:
            self.action(value, packet, directory)
        return value


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CounterExecutorTests(TestCase):
    setUp = fixture.ProofArchitectureTests.setUp

    def first(self):
        snapshot = self.job.input_snapshot
        classification = classify_snapshot(snapshot, provenance_report=build_provenance_report(snapshot))
        preflight = build_preflight(snapshot, classification, rights_report=rights_fixture(snapshot))
        path, plan = shared.prepare_plan(self.job.pk, user=self.user, model='SYNTH', effort='none',
                                        classification=classification, preflight=preflight)
        allocation = self.allocate(plan, self.user, 'SYNTH:first')
        receipt = shared.execute_plan(path, user=self.user, allocation_path=allocation,
                                      execution_id='SYNTH:first', runner=SynthRunner(plan))
        self.classification = classification
        self.first_receipt = receipt
        self.counter_user = get_user_model().objects.create_user('SYNTH_independent_counter')
        UserProfile.objects.create(user=self.counter_user, role='verifier', is_test_identity=True)
        return receipt

    def allocate(self, plan, user, label='SYNTH:counter', ceiling=None):
        allocation = {'contract': shared.ALLOCATION, 'allocation_id': label,
            'authorization': 'explicit_dv_verite', 'authority_reference': 'SYNTH test allocation only',
            'owner_id': user.pk, 'plan_digests': [plan['plan_digest']], 'fingerprints': [plan['fingerprint']],
            'job_ids': [plan['job_id']], 'model': plan['model'], 'effort': plan['effort'],
            'max_phases': ceiling or plan['allocation_proposal']['proposed_ceiling'],
            'expires_at': (timezone.now()+timedelta(hours=1)).isoformat()}
        path = self.root/'plans'/(label.replace(':', '-')+'.json')
        shared._write(path, allocation)
        return path

    def prepare(self, max_claims=8):
        if not hasattr(self, 'first_receipt'):
            self.first()
        path, plan = executor.prepare_adversarial_plan(self.revision.pk, user=self.counter_user,
            first_receipt_id=self.first_receipt.pk, model='SYNTH', effort='none', max_claims=max_claims)
        allocation = self.allocate(plan, self.counter_user)
        return path, plan, allocation

    def run_counter(self, path, allocation, runner=None, execution_id='SYNTH:counter', **kwargs):
        return executor.execute_adversarial_plan(path, user=self.counter_user, allocation_path=allocation,
            execution_id=execution_id, runner=runner or AdversarialSynth(), **kwargs)

    def native(self):
        return architecture._native_semantics(self.revision, self.classification)

    def test_real_service_binding_immutable_first_receipt_and_read_only_gate(self):
        path, plan, allocation = self.prepare()
        first_before = deepcopy(self.first_receipt.attempt.result)
        first_job_state = self.first_receipt.job.state
        runner = AdversarialSynth()
        artifact = self.run_counter(path, allocation, runner)
        report = artifact.payload
        self.assertEqual(len(runner.calls), len(plan['batches']))
        self.assertTrue(report['summary']['counterevidence_complete'])
        self.assertTrue(report['runtime_identity_verified'])
        self.assertEqual(report['origin'], 'SYNTH_qualified_receipts')
        self.assertEqual(len(report['axes']), 8)
        self.assertTrue(all(a['absence_proven'] is False for a in report['axes']))
        self.first_receipt.refresh_from_db(); self.first_receipt.attempt.refresh_from_db(); self.first_receipt.job.refresh_from_db()
        self.assertEqual(self.first_receipt.attempt.result, first_before)
        self.assertEqual(self.first_receipt.job.state, first_job_state)
        self.assertEqual(VerificationReceipt.objects.count(), 1)
        self.assertEqual(HumanReview.objects.count(), 0)
        self.assertEqual(VerificationDelivery.objects.count(), 0)
        native = self.native()
        with CaptureQueriesContext(connection) as queries:
            received = executor.received_counter_for_revision(self.revision, self.classification, native)
        self.assertEqual(received, report)
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in {'INSERT','UPDATE','DELETE','ALTER','CREATE','DROP'} for q in queries.captured_queries))
        self.assertFalse(report['summary']['publishable'])

    def test_prepare_never_creates_counter_attempt_or_allocation(self):
        self.first()
        attempts = VerificationAttempt.objects.count()
        with patch.object(executor, 'AdversarialCLI') as cli, patch.object(shared, '_allocation') as allocate:
            _, plan = executor.prepare_adversarial_plan(self.revision.pk, user=self.counter_user,
                first_receipt_id=self.first_receipt.pk, model='SYNTH', effort='none')
            cli.assert_not_called(); allocate.assert_not_called()
        self.assertEqual(VerificationAttempt.objects.count(), attempts)
        self.assertEqual(plan['allocation_proposal']['actual_phases_reserved'], 0)

    def test_missing_generation_actor_or_same_first_actor_refused_before_allocation(self):
        self.first()
        with self.assertRaisesRegex(ValidationError, 'separation_missing'):
            executor.prepare_adversarial_plan(self.revision.pk, user=self.user,
                first_receipt_id=self.first_receipt.pk, model='SYNTH', effort='none')
        self.revision.created_by = None; self.revision.save(update_fields=['created_by'])
        with patch.object(shared, '_allocation') as allocate, patch.object(executor, 'AdversarialCLI') as cli:
            with self.assertRaisesRegex(ValidationError, 'actor_or_separation_missing'):
                executor.prepare_adversarial_plan(self.revision.pk, user=self.counter_user,
                    first_receipt_id=self.first_receipt.pk, model='SYNTH', effort='none')
            allocate.assert_not_called(); cli.assert_not_called()

    def test_missing_preflight_or_stale_source_refused_before_allocation(self):
        path, _, allocation = self.prepare()
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM atelier_proofartifact WHERE schema = %s', ['dv.verification-preflight/1'])
        with patch.object(shared, '_allocation') as allocate, patch.object(executor, 'AdversarialCLI') as cli:
            with self.assertRaisesRegex(ValidationError, 'preflight_not_received'):
                self.run_counter(path, allocation)
            allocate.assert_not_called(); cli.assert_not_called()
        self.assertEqual(VerificationAttempt.objects.count(), 1)

    def test_allocation_missing_refused_before_constructor_and_counter_attempt(self):
        path, _, _ = self.prepare()
        with patch.object(executor, 'AdversarialCLI') as cli:
            with self.assertRaisesRegex(ExecutionFailure, 'explicit_unconsumed_allocation_required'):
                executor.execute_adversarial_plan(path, user=self.counter_user, allocation_path=None,
                    execution_id='SYNTH:counter')
            cli.assert_not_called()
        self.assertEqual(VerificationAttempt.objects.count(), 1)

    def test_manually_prepared_legacy_first_result_cannot_bypass_received_preflight(self):
        path, plan, allocation = self.prepare()
        # Fault injection is confined to this fresh SYNTH memory database.
        legacy = plan['first_result']; digest = sha256_digest(legacy)
        VerificationAttempt.objects.filter(pk=self.first_receipt.attempt_id).update(result=legacy, result_digest=digest)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE atelier_verificationreceipt SET result_digest = %s WHERE id = %s', [digest, self.first_receipt.pk])
        with patch.object(shared, '_allocation') as allocate, patch.object(executor, 'AdversarialCLI') as cli:
            with self.assertRaisesRegex(ValidationError, 'layered_first_receipt_with_received_preflight_required'):
                self.run_counter(path, allocation)
            allocate.assert_not_called(); cli.assert_not_called()
        self.assertEqual(VerificationAttempt.objects.count(), 1)

    def test_replay_zero_new_calls_no_phase_or_terminal_prefix_rewrite(self):
        path, plan, allocation = self.prepare()
        original = self.run_counter(path, allocation)
        budget = shared._read(allocation, external=True)
        before = shared._allocation_snapshot(budget)
        runner = AdversarialSynth()
        replay = self.run_counter(path, allocation, runner)
        self.assertEqual(replay.pk, original.pk)
        self.assertEqual(runner.calls, [])
        self.assertEqual(shared._allocation_snapshot(budget), before)
        self.assertEqual(VerificationAttempt.objects.count(), 2)

    def test_checkpoint_closes_lease_resumes_exact_prefix_with_new_execution(self):
        path, plan, allocation = self.prepare(max_claims=1)
        with self.assertRaisesRegex(ExecutionFailure, 'adversarial_checkpoint_received'):
            self.run_counter(path, allocation, stop_after_accepted_batches=1)
        attempt = VerificationAttempt.objects.order_by('-pk').first()
        self.assertEqual(attempt.stage, 'interrupted'); self.assertIsNotNone(attempt.finished_at)
        budget = shared._read(allocation, external=True)
        first_phase = deepcopy(shared._allocation_snapshot(budget)['phases'][0])
        runner = AdversarialSynth()
        artifact = self.run_counter(path, allocation, runner, execution_id='SYNTH:counter-resume')
        phases = shared._allocation_snapshot(budget)['phases']
        self.assertEqual(phases[0], first_phase)
        self.assertEqual(len(phases), len(plan['batches']))
        self.assertEqual(len(runner.calls), len(plan['batches'])-1)
        self.assertTrue(artifact.payload['summary']['counterevidence_complete'])

    def test_lost_journal_ack_recovers_accepted_phase_without_new_charge(self):
        path, plan, allocation = self.prepare(max_claims=1)
        actual_ack = shared._ack_phase
        lost = []
        def ack(*args, **kwargs):
            result = actual_ack(*args, **kwargs)
            if args[2].get('state') == 'accepted' and not lost:
                lost.append(True)
                raise ExecutionFailure('SYNTH_lost_ack')
            return result
        with patch.object(shared, '_ack_phase', side_effect=ack):
            with self.assertRaisesRegex(ExecutionFailure, 'SYNTH_lost_ack'):
                self.run_counter(path, allocation)
        budget = shared._read(allocation, external=True)
        terminal = deepcopy(shared._allocation_snapshot(budget)['phases'][0])
        runner = AdversarialSynth()
        artifact = self.run_counter(path, allocation, runner, execution_id='SYNTH:counter-recovery')
        self.assertEqual(shared._allocation_snapshot(budget)['phases'][0], terminal)
        self.assertEqual(len(runner.calls), len(plan['batches'])-1)
        self.assertTrue(artifact.payload['runtime_identity_verified'])

    def test_ambiguous_effect_without_return_blocks_no_retry(self):
        path, plan, allocation = self.prepare()
        def fail(value, packet, directory):
            raise ExecutionFailure('SYNTH_ambiguous_effect')
        with self.assertRaisesRegex(ExecutionFailure, 'SYNTH_ambiguous_effect'):
            self.run_counter(path, allocation, AdversarialSynth(action=fail))
        runner = AdversarialSynth()
        with self.assertRaisesRegex(ExecutionFailure, 'effect_ambiguous'):
            self.run_counter(path, allocation, runner, execution_id='SYNTH:counter-refused')
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(shared._allocation_snapshot(shared._read(allocation, external=True))['phases']), 1)

    def test_tampered_raw_or_revoked_runtime_actor_invalidates_received_counter(self):
        path, plan, allocation = self.prepare()
        artifact = self.run_counter(path, allocation)
        native = self.native()
        UserProfile.objects.filter(user=self.counter_user).update(active=False)
        self.assertIsNone(executor.received_counter_for_revision(self.revision, self.classification, native))
        UserProfile.objects.filter(user=self.counter_user).update(active=True)
        raw = executor._directory(plan)/plan['batches'][0]['batch_id']/'1'/'raw-return.json'
        raw.write_bytes(raw.read_bytes()+b' ')
        self.assertIsNone(executor.received_counter_for_revision(self.revision, self.classification, native))
        self.assertEqual(ProofArtifact.objects.filter(kind='counterevidence').count(), 1)

    def test_new_first_receipt_identity_same_fingerprint_invalidates_counter(self):
        path, _, allocation = self.prepare()
        self.run_counter(path, allocation)
        native = self.native(); native['receipt_id'] += 100
        self.assertIsNone(executor.received_counter_for_revision(self.revision, self.classification, native))

    def test_eight_axes_and_exact_chosen_support_cannot_be_replaced(self):
        path, plan, _ = self.prepare()
        batch = plan['batches'][0]
        raw = raw_response(executor._packet(plan, batch))
        self.assertEqual(counter.validate_adversarial_response(plan, batch, raw), raw)
        bad = deepcopy(raw); bad['axes'].pop()
        with self.assertRaisesRegex(ValidationError, 'eight_axes_required'):
            counter.validate_adversarial_response(plan, batch, bad)
        chosen = next((j for j in raw['claims'] if j['support_relations']), None)
        self.assertIsNotNone(chosen)
        chosen['support_relations'][0]['quote'] += ' SYNTH neighboring substitution'
        with self.assertRaisesRegex(ValidationError, 'support_replaced'):
            counter.validate_adversarial_response(plan, batch, raw)

    def test_unfavorable_support_stays_objection_without_first_verdict_rewrite(self):
        path, plan, allocation = self.prepare()
        first = deepcopy(self.first_receipt.attempt.result)
        def qualify(value, packet, directory):
            for judgment in value['claims']:
                if judgment['support_relations']:
                    judgment['support_relations'][0]['relation'] = 'context_only'
                    judgment['conclusion'] = 'qualified'
                    break
        artifact = self.run_counter(path, allocation, AdversarialSynth(action=qualify))
        self.assertGreater(artifact.payload['summary']['finding_count'], 0)
        self.assertTrue(any(f['code'] == 'adversarial_selected_support_context_only' for f in artifact.payload['findings']))
        self.first_receipt.attempt.refresh_from_db()
        self.assertEqual(self.first_receipt.attempt.result, first)
        self.assertFalse(artifact.payload['summary']['publishable'])

    def test_cost_counter_eight_claims_not_first_pass_thirtytwo(self):
        self.first()
        classification = deepcopy(self.classification)
        template = next(c for c in classification['claims'] if c['layer'] == 'semantic')
        classification['claims'] = [dict(template, claim_id='SYNTH-'+str(i)) for i in range(65)]
        cost = estimate_cost(self.job.input_snapshot, classification)
        self.assertEqual(cost['semantic_model_phases'], 3)
        self.assertEqual(cost['counterevidence_model_phases'], 9)
        self.assertEqual(cost['counter_max_claims_per_phase'], 8)
        _, plan, _ = self.prepare()
        producer = architecture.received_plan(self.first_receipt.job, self.first_receipt.attempt.result)
        self.assertEqual(producer['allocation_proposal']['counterevidence_phases'], len(plan['batches']))
