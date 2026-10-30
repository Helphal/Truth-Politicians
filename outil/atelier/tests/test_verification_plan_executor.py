"""SYNTH Django service/command/recovery tests. No provider or real database."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from atelier.models import HumanReview, UserProfile, VerificationAttempt, VerificationReceipt
from atelier.services import verification as v
from atelier.services import verification_protocol as p
from atelier.services import verification_plan_executor as x
from atelier.services.verification_executor import CodexCLI, ExecutionFailure
from atelier.services.canonical import sha256_digest
from atelier.tests import test_verification as fixtures
from atelier.tests.test_verification_protocol import claims_raw, evidence_raw


class SynthRunner:
    SYNTH = True
    identity = {'model': 'SYNTH', 'model_reasoning_effort': 'none', 'fixture': 'SYNTH_no_provider'}

    def __init__(self, plan, action=None, verdict='ETAYE_DANS_PERIMETRE'):
        self.plan = plan; self.calls = []; self.action = action; self.verdict = verdict

    def run(self, stage, packet, directory, check):
        self.calls.append((stage, packet['batch_id']))
        if self.action:
            self.action(stage, packet, directory)
        check()
        batch = next(b for b in self.plan['batches'] if b['batch_id'] == packet['batch_id'])
        return evidence_raw(self.plan, batch) if stage == 'evidence' else claims_raw(self.plan, batch, self.verdict)


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PlanExecutorTests(TestCase):
    setUpFixture = fixtures.VerificationTests.setUp

    def setUp(self):
        self.setUpFixture()
        self.tmp = tempfile.TemporaryDirectory(prefix='dv-plan-SYNTH-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = override_settings(DV_VERIFICATION_PLAN_ROOT=self.root)
        self.settings.enable(); self.addCleanup(self.settings.disable)
        self.path, self.plan = x.prepare_plan(self.job.pk, user=self.user, model='SYNTH', effort='none',
                                            limits=replace(p.Limits(), max_claims=2))
        self.allocation_path = self.root / 'SYNTH-allocation.json'
        self.allocation = {'contract': x.ALLOCATION, 'allocation_id': 'SYNTH:allocation1',
            'authorization': 'explicit_dv_verite', 'authority_reference': 'SYNTH fixture, no real authority',
            'owner_id': self.user.pk, 'plan_digests': [self.plan['plan_digest']],
            'fingerprints': [self.plan['fingerprint']], 'job_ids': [self.job.pk],
            'model': 'SYNTH', 'effort': 'none',
            'max_phases': self.plan['allocation_proposal']['proposed_ceiling'],
            'expires_at': (timezone.now()+timedelta(hours=1)).isoformat()}
        x._write(self.allocation_path, self.allocation)

    def execute(self, runner=None, **kwargs):
        return x.execute_plan(self.path, user=self.user, allocation_path=self.allocation_path,
                              execution_id='SYNTH:bounded-plan', runner=runner or SynthRunner(self.plan), **kwargs)

    def journal(self):
        return x._read(x._state_directory(self.plan)/'journal.json')

    def ledger(self):
        return x._read(self.root/'allocations'/sha256_digest(self.allocation['allocation_id'])/'ledger.json')

    def test_prepare_is_immutable_and_does_not_start_or_reserve(self):
        self.assertEqual(self.job.attempts.count(),0)
        self.assertFalse((self.root/'allocations').exists())
        self.assertEqual(self.plan['allocation_proposal']['authorization'],'NOT_AUTHORIZED')
        again,plan=x.prepare_plan(self.job.pk,user=self.user,model='SYNTH',effort='none',limits=replace(p.Limits(),max_claims=2))
        self.assertEqual(again,self.path); self.assertEqual(plan,self.plan)

    def test_complete_partition_real_service_receipt_but_no_review_or_publication(self):
        runner=SynthRunner(self.plan); receipt=self.execute(runner)
        self.assertEqual(receipt.decision,'supported')
        self.assertEqual(len(runner.calls),self.plan['allocation_proposal']['minimum_new_phases'])
        self.assertEqual(HumanReview.objects.count(),0)
        self.assertEqual(self.journal()['result']['diffusion'],'not_authorized')
        self.assertTrue(all(phase['state']=='accepted' for phase in self.ledger()['phases']))

    def test_negative_result_is_complete_received_and_remains_blocked(self):
        receipt=self.execute(SynthRunner(self.plan,verdict='CONTREDIT'))
        self.assertEqual(receipt.decision,'not_supported')
        self.assertEqual(self.journal()['result']['execution'],'complete')
        self.assertFalse(v.gate(self.revision)[0]); self.assertEqual(HumanReview.objects.count(),0)

    def test_missing_expired_wrong_scope_and_small_allocation_never_construct_cli(self):
        with patch.object(x,'ProtocolCLI') as factory:
            with self.assertRaisesRegex(ExecutionFailure,'allocation_required'):
                x.execute_plan(self.path,user=self.user,allocation_path=None,execution_id='SYNTH:missing')
            factory.assert_not_called()
        for change in ('expired','scope','owner','configuration','ceiling'):
            bad=deepcopy(self.allocation)
            if change=='expired': bad['expires_at']=(timezone.now()-timedelta(seconds=1)).isoformat()
            if change=='scope': bad['plan_digests']=['b'*64]
            if change=='owner': bad['owner_id']=self.editor.pk
            if change=='configuration': bad['model']='changed'
            if change=='ceiling': bad['max_phases']=1
            x._write(self.allocation_path,bad)
            with self.subTest(change=change),patch.object(x,'ProtocolCLI') as factory:
                with self.assertRaises(ExecutionFailure):
                    x.execute_plan(self.path,user=self.user,allocation_path=self.allocation_path,execution_id='SYNTH:refuse')
                factory.assert_not_called()
            # A budget identity is frozen once persisted; use a fresh temporary
            # allocation name for the next independent refusal witness.
            self.allocation['allocation_id'] += 'x'
        self.assertEqual(self.job.attempts.count(),0)

    def test_completed_replay_has_zero_provider_calls(self):
        receipt=self.execute(); runner=SynthRunner(self.plan)
        replay=self.execute(runner)
        self.assertEqual(replay.pk,receipt.pk); self.assertEqual(runner.calls,[])

    def test_synthetic_runner_needs_test_environment_identity_and_unchanged_configuration(self):
        runner=SynthRunner(self.plan)
        with override_settings(DV_ENV='local'):
            with self.assertRaisesRegex(ExecutionFailure,'outside_synth_test_scope'): self.execute(runner)
        UserProfile.objects.filter(user=self.user).update(is_test_identity=False)
        self.user.refresh_from_db()
        with self.assertRaisesRegex(ExecutionFailure,'outside_synth_test_scope'): self.execute(runner)
        UserProfile.objects.filter(user=self.user).update(is_test_identity=True)
        self.user.refresh_from_db()
        runner.identity={**runner.identity,'model':'SYNTH-changed'}
        with self.assertRaisesRegex(ExecutionFailure,'configuration_changed'): self.execute(runner)
        self.assertEqual(runner.calls,[]); self.assertEqual(self.job.attempts.count(),0)

    def test_stop_file_refuses_before_constructor_and_consumes_no_phase(self):
        stop=self.root/'STOP'; stop.touch()
        with patch.object(x,'ProtocolCLI') as factory:
            with self.assertRaisesRegex(ExecutionFailure,'interrupted'):
                x.execute_plan(self.path,user=self.user,allocation_path=self.allocation_path,
                    execution_id='SYNTH:stop',stop_file=stop)
            factory.assert_not_called()
        self.assertEqual(self.ledger()['phases'],[]); self.assertEqual(self.job.attempts.count(),0)

    def test_running_attempt_of_another_plan_preserved_before_cli_constructor(self):
        existing=v.start(self.job.pk,user=self.user,execution_id='SYNTH:other-owner-operation',provider='synthetic')
        with patch.object(x,'ProtocolCLI') as factory:
            with self.assertRaisesRegex(ExecutionFailure,'unattributed_running_attempt'):
                x.execute_plan(self.path,user=self.user,allocation_path=self.allocation_path,execution_id='SYNTH:bounded-plan')
            factory.assert_not_called()
        existing.refresh_from_db(); self.assertIsNone(existing.finished_at)
        self.assertEqual(self.job.attempts.count(),1); self.assertEqual(self.ledger()['phases'],[])

    def test_crash_after_business_commit_recovers_receipt_without_second_read_or_generation(self):
        real_finish=v.finish
        def commit_then_crash(*args,**kwargs):
            real_finish(*args,**kwargs)
            raise SystemExit('SYNTH power loss after business commit')
        with patch.object(v,'finish',side_effect=commit_then_crash):
            with self.assertRaises(SystemExit): self.execute()
        self.assertEqual(VerificationReceipt.objects.count(),1)
        runner=SynthRunner(self.plan); receipt=self.execute(runner)
        self.assertEqual(receipt.pk,VerificationReceipt.objects.get().pk); self.assertEqual(runner.calls,[])

    def test_crash_after_return_receipt_before_journal_return_reuses_output(self):
        real_persist=x._persist_return
        def persist_then_crash(*args,**kwargs):
            real_persist(*args,**kwargs)
            raise SystemExit('SYNTH lost return journal ACK')
        with patch.object(x,'_persist_return',side_effect=persist_then_crash):
            with self.assertRaises(SystemExit): self.execute()
        runner=SynthRunner(self.plan); receipt=self.execute(runner)
        self.assertEqual(receipt.decision,'supported')
        self.assertTrue(all(stage=='claims' for stage,_ in runner.calls))
        self.assertEqual(len(self.ledger()['phases']),self.plan['allocation_proposal']['minimum_new_phases'])

    def test_crash_before_business_receipt_recovers_all_returned_batches_without_generation(self):
        with patch.object(v,'finish',side_effect=SystemExit('SYNTH before business commit')):
            with self.assertRaises(SystemExit): self.execute()
        self.assertEqual(VerificationReceipt.objects.count(),0)
        runner=SynthRunner(self.plan)
        receipt=self.execute(runner)
        self.assertEqual(receipt.decision,'supported'); self.assertEqual(runner.calls,[])

    def test_crash_after_ledger_reservation_before_local_ack_keeps_spent_phase(self):
        original_write=x._write
        fired=False
        def break_before_local_ack(path,value):
            nonlocal fired
            if Path(path).name=='journal.json' and isinstance(value,dict):
                states=value.get('batches',{}).values()
                if not fired and any(s['state']=='reserved' for s in states):
                    fired=True
                    raise SystemExit('SYNTH reservation durable before local journal ACK')
            return original_write(path,value)
        with patch.object(x,'_write',side_effect=break_before_local_ack):
            with self.assertRaises(SystemExit): self.execute()
        self.assertEqual(len(self.ledger()['phases']),1)
        runner=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'explicit_new_operation'):
            self.execute(runner)
        self.assertEqual(runner.calls,[])
        receipt=self.execute(runner,retry_batches=[self.plan['batches'][0]['batch_id']])
        self.assertEqual(receipt.decision,'supported')
        self.assertEqual(len(self.ledger()['phases']),self.allocation['max_phases'])
        ids=[phase['phase_id'] for phase in self.ledger()['phases']]
        self.assertEqual(len(ids),len(set(ids)))

    def test_ledger_launch_before_local_ack_is_ambiguous_not_free_reserved_retry(self):
        original_write=x._write
        fired=False
        def break_before_launch_ack(path,value):
            nonlocal fired
            if Path(path).name=='journal.json' and isinstance(value,dict):
                if not fired and any(s['state']=='launched' for s in value.get('batches',{}).values()):
                    fired=True
                    raise SystemExit('SYNTH launch ledger persisted before local launch ACK')
            return original_write(path,value)
        with patch.object(x,'_write',side_effect=break_before_launch_ack):
            with self.assertRaises(SystemExit): self.execute()
        runner=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'ambiguous'):
            self.execute(runner,retry_batches=[self.plan['batches'][0]['batch_id']])
        self.assertEqual(runner.calls,[]); self.assertEqual(len(self.ledger()['phases']),1)

    def test_attempt_lease_expiry_during_return_stops_remaining_phases(self):
        def expire(stage,packet,directory):
            from atelier.models import VerificationAttempt
            VerificationAttempt.objects.filter(job=self.job).update(expires_at=timezone.now()-timedelta(seconds=1))
        runner=SynthRunner(self.plan,action=expire)
        with self.assertRaisesRegex(ValidationError,'closed_or_expired'): self.execute(runner)
        self.assertEqual(len(runner.calls),1); self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_crash_during_provider_launch_is_ambiguous_and_never_replayed(self):
        def crash(stage,packet,directory): raise SystemExit('SYNTH effect unknowable')
        with self.assertRaises(SystemExit): self.execute(SynthRunner(self.plan,action=crash))
        runner=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'ambiguous'):
            self.execute(runner,retry_batches=[self.plan['batches'][0]['batch_id']])
        self.assertEqual(runner.calls,[]); self.assertEqual(len(self.ledger()['phases']),1)

    def test_timeout_is_consumed_and_new_operation_counted(self):
        def timeout(stage,packet,directory): raise ExecutionFailure('timeout')
        with self.assertRaises(ExecutionFailure): self.execute(SynthRunner(self.plan,action=timeout))
        runner=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'explicit_new_operation'):
            self.execute(runner)
        self.assertEqual(runner.calls,[]); self.assertEqual(len(self.ledger()['phases']),1)
        receipt=self.execute(SynthRunner(self.plan),retry_batches=[self.plan['batches'][0]['batch_id']])
        self.assertEqual(receipt.decision,'supported')
        phases=self.ledger()['phases']; self.assertEqual(len(phases),self.allocation['max_phases'])
        self.assertEqual(phases[1]['retry_of'],phases[0]['phase_id'])
        self.assertEqual(phases[0]['state'],'interrupted')

    def test_truncated_json_preserved_as_refusal_without_automatic_retry(self):
        runner=SynthRunner(self.plan); runner.run=lambda *args: b'{"SYNTH":'
        with self.assertRaisesRegex(ExecutionFailure,'malformed_persisted_json'): self.execute(runner)
        self.assertEqual(len(self.ledger()['phases']),1)
        journal=self.journal(); state=next(iter(journal['batches'].values()))
        self.assertEqual(state['state'],'refused')
        path=x._state_directory(self.plan)/self.plan['batches'][0]['batch_id']/'1'/'raw-return.json'
        self.assertEqual(path.read_bytes(),b'{"SYNTH":')
        self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_duplicate_json_member_is_preserved_and_refused(self):
        runner=SynthRunner(self.plan); runner.run=lambda *args: b'{"protocol":"SYNTH","protocol":"hidden-conflict"}'
        with self.assertRaisesRegex(ExecutionFailure,'malformed_persisted_json'): self.execute(runner)
        self.assertEqual(len(self.ledger()['phases']),1)
        self.assertEqual(next(iter(self.journal()['batches'].values()))['state'],'refused')
        self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_partial_claims_saved_and_not_accepted(self):
        runner=SynthRunner(self.plan); normal=runner.run
        def missing(stage,*args):
            raw=normal(stage,*args)
            if stage=='claims': raw['claims'].pop()
            return raw
        runner.run=missing
        with self.assertRaisesRegex(ValidationError,'incomplete_claim_coverage'): self.execute(runner)
        self.assertEqual(VerificationReceipt.objects.count(),0)
        states=self.journal()['batches']
        self.assertEqual(states['evidence-0000']['state'],'accepted')
        self.assertEqual(states['claims-0000']['state'],'partial')

    def test_provider_unavailable_stops_before_remaining_batches(self):
        def fail(stage,packet,directory): raise ExecutionFailure('provider_unavailable')
        runner=SynthRunner(self.plan,action=fail)
        with self.assertRaises(ExecutionFailure): self.execute(runner)
        self.assertEqual(len(runner.calls),1); self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_authority_revoked_during_call_refuses(self):
        def mutate(stage,packet,directory):
            UserProfile.objects.filter(user=self.user).update(active=False)
        with self.assertRaises(PermissionDenied):
            self.execute(SynthRunner(self.plan,action=mutate))
        self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_source_changes_during_call_refuse_late_return(self):
        def mutate(stage,packet,directory):
            self.passage.context='SYNTH changed context'; self.passage.save()
        with self.assertRaises(ValidationError): self.execute(SynthRunner(self.plan,action=mutate))
        self.assertEqual(VerificationReceipt.objects.count(),0)

    def test_wrong_owner_writer_and_changed_fingerprint_refuse_before_call(self):
        runner=SynthRunner(self.plan)
        with self.assertRaises(PermissionDenied):
            x.execute_plan(self.path,user=self.editor,allocation_path=self.allocation_path,execution_id='SYNTH:wrong',runner=runner)
        self.revision.title='SYNTH changed revision'; self.revision.save()
        with self.assertRaises(ValidationError): self.execute(runner)
        self.assertEqual(runner.calls,[])

    def test_source_read_reuse_survives_refused_claims_and_is_attributed(self):
        def reject_claims(stage,packet,directory):
            if stage=='claims': raise ExecutionFailure('provider_unavailable')
        with self.assertRaises(ExecutionFailure):
            self.execute(SynthRunner(self.plan,action=reject_claims))
        self.assertEqual(VerificationReceipt.objects.count(),0)
        # Same frozen sources, a separate SYNTH object/revision and genuine
        # separate current job. The provider is never called for the sources.
        from atelier.models import EditorialDossier, EditorialRevision
        dossier=EditorialDossier.objects.create(stable_id='SYNTH-OTHER',content_kind='speech_vote',
            established_kind='speech_vote',data_scope='synthetic',current_revision=1)
        revision=EditorialRevision.objects.create(dossier=dossier,revision=1,title='SYNTH alternative',
            public_payload={'title':'SYNTH alternative'},snapshot_digest=sha256_digest({'title':'SYNTH alternative'}),
            verification_required=True,source_snapshot=deepcopy(self.revision.source_snapshot),created_by=self.editor)
        job=v.enqueue(revision)
        path,plan=x.prepare_plan(job.pk,user=self.user,model='SYNTH',effort='none',
                                  source_reuse_plan_digest=self.plan['plan_digest'])
        allocation=deepcopy(self.allocation); allocation.update(allocation_id='SYNTH:allocation2',
            plan_digests=[plan['plan_digest']],fingerprints=[plan['fingerprint']],job_ids=[job.pk],
            max_phases=plan['allocation_proposal']['proposed_ceiling'])
        alloc=self.root/'SYNTH-alloc2.json'; x._write(alloc,allocation)
        runner=SynthRunner(plan)
        receipt=x.execute_plan(path,user=self.user,allocation_path=alloc,execution_id='SYNTH:reuse',runner=runner)
        self.assertEqual(receipt.decision,'supported'); self.assertTrue(all(s=='claims' for s,_ in runner.calls))
        journal=x._read(x._state_directory(plan)/'journal.json')
        self.assertFalse(journal['source_reuse']['independent_reading'])
        self.assertEqual(journal['source_reuse']['plan_digest'],self.plan['plan_digest'])
        self.assertEqual(VerificationReceipt.objects.count(),1)

    def test_completed_artifact_tamper_refuses(self):
        self.execute(); state=next(iter(self.journal()['batches'].values()))
        directory=x._state_directory(self.plan)/state['batch_id']/str(state['run_number'])
        raw=x._read(directory/'raw-return.json'); raw['limitations'].append('SYNTH altered')
        x._write(directory/'raw-return.json',raw)
        runner=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'binding_mismatch'): self.execute(runner)
        self.assertEqual(runner.calls,[])

    def test_allocation_ceiling_cannot_be_changed_under_same_identity(self):
        self.execute(); self.allocation['max_phases']+=1; x._write(self.allocation_path,self.allocation)
        with self.assertRaisesRegex(ExecutionFailure,'identity_or_ceiling_changed'): self.execute()

    def test_plan_lock_blocks_different_allocation_before_generation(self):
        runner=SynthRunner(self.plan)
        self.allocation['allocation_id']='SYNTH:different-allocation'; x._write(self.allocation_path,self.allocation)
        with x._lock(x._state_directory(self.plan)/'lock'):
            with self.assertRaisesRegex(ExecutionFailure,'already_running'): self.execute(runner)
        self.assertEqual(runner.calls,[]); self.assertEqual(self.job.attempts.count(),0)

    def test_management_prepare_and_execute_are_wired_to_actual_service(self):
        stdout=StringIO()
        call_command('prepare_verification_plan',job=self.job.pk,user_id=self.user.pk,model='SYNTH',effort='none',max_claims=2,stdout=stdout)
        value=json.loads(stdout.getvalue()); self.assertEqual(value['plan_digest'],self.plan['plan_digest'])
        self.assertFalse(value['provider_called'])
        with patch('atelier.management.commands.execute_verification_plan.execute_plan',wraps=x.execute_plan) as service:
            with self.assertRaises(CommandError):
                call_command('execute_verification_plan',plan=str(self.path),user_id=self.user.pk,
                    execution_id='SYNTH:command',allocation=str(self.root/'absent.json'),stdout=StringIO())
            self.assertEqual(service.call_count,1)
        self.assertEqual(self.job.attempts.count(),0)
        runner=SynthRunner(self.plan)
        def service_with_synth(*args,**kwargs):
            return x.execute_plan(*args,**kwargs,runner=runner)
        stdout=StringIO()
        with patch('atelier.management.commands.execute_verification_plan.execute_plan',side_effect=service_with_synth) as service:
            call_command('execute_verification_plan',plan=str(self.path),user_id=self.user.pk,
                execution_id='SYNTH:command',allocation=str(self.allocation_path),stdout=stdout)
            self.assertEqual(service.call_count,1)
        self.assertEqual(json.loads(stdout.getvalue())['decision'],'supported')
        self.assertEqual(len(runner.calls),self.plan['allocation_proposal']['minimum_new_phases'])


    def test_documentary_management_input_is_measured_and_bound_to_actual_runtime_packet(self):
        import hashlib
        directory = self.root/'documentary-inputs'; directory.mkdir()
        source_path = directory/'SYNTH-context.json'
        bundle = {'contract': p.DOCUMENTARY_INPUT, 'fingerprint': self.job.fingerprint,
            'records': [{'family': 'editorial_perimeter', 'purpose': 'SYNTH contexte déclaré.',
                        'locations': ['/title'], 'proof': {'kind': 'snapshot_pointer', 'pointer': '/title',
                                                        'expected': self.revision.title}}]}
        x._write(source_path, bundle)
        stdout = StringIO()
        call_command('prepare_verification_plan', job=self.job.pk, user_id=self.user.pk,
            model='SYNTH', effort='none', max_claims=2, documentary_input=str(source_path), stdout=stdout)
        output = json.loads(stdout.getvalue()); plan = x._read(output['plan_path'])
        self.assertEqual(plan['documentary_annex']['fingerprint'], self.job.fingerprint)
        self.assertFalse(output['provider_called']); self.assertEqual(self.job.attempts.count(), 0)
        allocation = deepcopy(self.allocation); allocation['plan_digests'] = [plan['plan_digest']]
        x._write(self.allocation_path, allocation)
        packets = []
        runner = SynthRunner(plan, action=lambda stage,packet,directory: packets.append(deepcopy(packet)))
        receipt = x.execute_plan(output['plan_path'], user=self.user, allocation_path=self.allocation_path,
            execution_id='SYNTH:annex-command', runner=runner)
        self.assertEqual(receipt.decision, 'supported')
        self.assertNotIn('documentary_evidence', packets[0])
        self.assertTrue(all(packet['documentary_evidence']==plan['documentary_annex']
                           for packet in packets if 'assertions' in packet))
        journal = x._read(x._state_directory(plan)/'journal.json')
        batch = next(b for b in plan['batches'] if b['stage']=='claims')
        state = journal['batches'][batch['batch_id']]
        actual_packet = next(packet for packet in packets if packet['batch_id']==batch['batch_id'])
        self.assertEqual(state['input_sha256'], hashlib.sha256(
            p.prompt_for('claims', actual_packet).encode('utf-8')).hexdigest())

    def test_documentary_preparer_confines_input_and_reads_only_pinned_original_version(self):
        import hashlib
        vault = self.root/'SYNTH-vault'; vault.mkdir()
        original = b'<html>SYNTH fait limite.</html>'
        digest = hashlib.sha256(original).hexdigest(); original_path = vault/'SYNTH-original.html'
        original_path.write_bytes(original)
        self.doc.sha256=digest; self.doc.original_path=str(original_path); self.doc.byte_size=len(original); self.doc.save()
        references = deepcopy(self.revision.source_snapshot); references[0]['sha256']=digest
        self.revision.source_snapshot=references; self.revision.save(); self.job=v.enqueue(self.revision)
        proof = {'kind':'pinned_original_segment', 'source_version':deepcopy(self.job.input_snapshot['sources'][0]['reference']),
            'original_sha256':digest, 'start':6, 'end':24, 'offset_unit':p.OFFSET_UNIT,
            'exact_segment':'SYNTH fait limite.'}
        bundle = {'contract':p.DOCUMENTARY_INPUT,'fingerprint':self.job.fingerprint,'records':[
            {'family':'institutional_fact','purpose':'SYNTH original distinct du passage.', 'locations':['/title'],'proof':proof}]}
        inputs = self.root/'documentary-inputs'; inputs.mkdir(); source_path=inputs/'SYNTH-original-input.json'
        x._write(source_path,bundle)
        with override_settings(DV_EVIDENCE_VAULT_PATH=vault):
            path,plan=x.prepare_plan(self.job.pk,user=self.user,model='SYNTH',effort='none',documentary_input_path=source_path)
            self.assertEqual(plan['documentary_annex']['records'][0]['proof']['segment_sha256'],hashlib.sha256(b'SYNTH fait limite.').hexdigest())
            self.assertEqual(x._current(plan,self.user).pk,self.job.pk)
            forged=deepcopy(plan['documentary_annex']); record=forged['records'][0]
            record['proof']['exact_segment']='SYNTH faux limite.'
            record['proof']['segment_sha256']=hashlib.sha256(b'SYNTH faux limite.').hexdigest()
            record_body=deepcopy(record); record_body.pop('record_id')
            record['record_id']='doc-'+sha256_digest([p.DOCUMENTARY_INPUT,forged['fingerprint'],record_body])
            body=deepcopy(forged); body.pop('annex_digest'); forged['annex_digest']=sha256_digest(body)
            p.validate_documentary_input(plan['snapshot'],forged)
            with self.assertRaisesRegex(ValidationError,'exact_proof_mismatch'):
                x._recheck_documentary(plan['snapshot'],forged)
            outside=self.root/'SYNTH-outside.html'; outside.write_bytes(original)
            self.doc.original_path=str(outside); self.doc.save()
            with self.assertRaisesRegex(ExecutionFailure,'outside_owned_root'):
                x._current(plan,self.user)
        outside_input=self.root/'SYNTH-outside-input.json'; x._write(outside_input,bundle)
        with self.assertRaisesRegex(ExecutionFailure,'outside_owned_root'):
            x.prepare_plan(self.job.pk,user=self.user,model='SYNTH',effort='none',documentary_input_path=outside_input)


    def test_graceful_stop_after_durable_return_resumes_without_regenerating_it(self):
        stop = self.root/'SYNTH-STOP'
        persist = x._persist_return
        def persist_then_stop(*args, **kwargs):
            receipt = persist(*args, **kwargs)
            stop.write_text('SYNTH controlled stop', encoding='utf-8')
            return receipt
        first = SynthRunner(self.plan)
        with patch.object(x, '_persist_return', side_effect=persist_then_stop):
            with self.assertRaisesRegex(ExecutionFailure, 'interrupted'):
                self.execute(first, stop_file=stop)
        batch = self.plan['batches'][0]
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(self.journal()['batches'][batch['batch_id']]['state'], 'returned')
        self.assertEqual(self.ledger()['phases'][0]['state'], 'returned')
        stop.unlink()
        resumed = SynthRunner(self.plan); receipt = self.execute(resumed, stop_file=stop)
        self.assertEqual(receipt.decision, 'supported')
        self.assertFalse(any(stage=='evidence' for stage,bid in resumed.calls))
        self.assertEqual(len(self.ledger()['phases']), self.plan['allocation_proposal']['minimum_new_phases'])
        self.assertTrue(all(phase['state']=='accepted' for phase in self.ledger()['phases']))

    def test_expired_lease_after_durable_return_keeps_output_for_new_owned_attempt(self):
        persist = x._persist_return
        def persist_then_expire(directory, value, phase, plan, runner):
            receipt = persist(directory, value, phase, plan, runner)
            VerificationAttempt.objects.filter(pk=phase['attempt_id']).update(
                expires_at=timezone.now()-timedelta(seconds=1))
            return receipt
        first = SynthRunner(self.plan)
        with patch.object(x, '_persist_return', side_effect=persist_then_expire):
            with self.assertRaisesRegex(ValidationError, 'attempt_closed_or_expired'):
                self.execute(first)
        self.assertEqual(self.ledger()['phases'][0]['state'], 'returned')
        resumed = SynthRunner(self.plan); receipt = self.execute(resumed)
        self.assertEqual(receipt.decision, 'supported')
        self.assertFalse(any(stage=='evidence' for stage,bid in resumed.calls))
        self.assertEqual(self.job.attempts.count(), 2)
        self.assertEqual(len(self.ledger()['phases']), self.plan['allocation_proposal']['minimum_new_phases'])
        self.assertTrue(all(phase['state']=='accepted' for phase in self.ledger()['phases']))

    def pilot_plan(self):
        self.path,self.plan=x.prepare_plan(self.job.pk,user=self.user,model='SYNTH',effort='none',
            limits=replace(p.Limits(),max_claims=1))
        self.allocation['plan_digests']=[self.plan['plan_digest']]
        self.allocation['max_phases']=self.plan['allocation_proposal']['proposed_ceiling']
        x._write(self.allocation_path,self.allocation)

    def test_pilot_receives_source_and_claim_then_stops_before_next_reservation(self):
        self.pilot_plan(); first=SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(first,stop_after_accepted_batches=2)
        self.assertEqual(len(first.calls),2)
        self.assertEqual([stage for stage,bid in first.calls],['evidence','claims'])
        self.assertEqual(len(self.ledger()['phases']),2)
        self.assertTrue(all(phase['state']=='accepted' for phase in self.ledger()['phases']))
        checkpoint=x._read(x._state_directory(self.plan)/'pilot-checkpoint.json')
        self.assertEqual(checkpoint['accepted_batches_in_operation'],2)
        self.assertFalse(checkpoint['receipt_created'])
        self.assertNotIn(checkpoint['next_batch'],self.journal()['batches'])
        self.assertEqual(VerificationReceipt.objects.count(),0)
        self.assertEqual(self.job.attempts.count(),1)
        resumed=SynthRunner(self.plan); receipt=self.execute(resumed)
        self.assertEqual(receipt.decision,'supported')
        self.assertEqual(len(resumed.calls),self.plan['allocation_proposal']['minimum_new_phases']-2)
        self.assertEqual(self.job.attempts.count(),1)
        self.assertEqual(len(self.ledger()['phases']),self.plan['allocation_proposal']['minimum_new_phases'])

    def test_pilot_at_last_batch_finishes_normally_and_invalid_limits_reserve_nothing(self):
        for value in (False,0,-1,'2',2.0):
            with self.subTest(value=value),self.assertRaisesRegex(ValidationError,'invalid_pilot_batch_limit'):
                self.execute(stop_after_accepted_batches=value)
        self.assertFalse((self.root/'allocations').exists())
        count=len(self.plan['batches']); runner=SynthRunner(self.plan)
        receipt=self.execute(runner,stop_after_accepted_batches=count)
        self.assertEqual(receipt.decision,'supported')
        self.assertEqual(len(runner.calls),self.plan['allocation_proposal']['minimum_new_phases'])
        self.assertFalse((x._state_directory(self.plan)/'pilot-checkpoint.json').exists())

    def test_management_pilot_reports_checkpoint_without_fake_receipt(self):
        self.pilot_plan(); runner=SynthRunner(self.plan)
        def service_with_synth(*args,**kwargs):
            return x.execute_plan(*args,**kwargs,runner=runner)
        stdout=StringIO()
        with patch('atelier.management.commands.execute_verification_plan.execute_plan',side_effect=service_with_synth):
            call_command('execute_verification_plan',plan=str(self.path),user_id=self.user.pk,
                execution_id='SYNTH:command-pilot',allocation=str(self.allocation_path),pilot_batches=2,stdout=stdout)
        output=json.loads(stdout.getvalue())
        self.assertEqual(output['status'],'pilot_checkpoint_received')
        self.assertIsNone(output['receipt_id'])
        self.assertEqual(len(runner.calls),2)
        self.assertEqual(len(self.ledger()['phases']),2)


class PlanExecutorBoundaryTests(SimpleTestCase):
    def test_path_dotdot_and_link_are_refused_and_owned_path_passes(self):
        with tempfile.TemporaryDirectory(prefix='dv-plan-SYNTH-path-') as directory:
            root=Path(directory)/'owned'; root.mkdir()
            with override_settings(DV_VERIFICATION_PLAN_ROOT=root):
                self.assertEqual(x._safe(root/'okay.json'),(root/'okay.json').resolve())
                with self.assertRaisesRegex(ExecutionFailure,'outside_owned_root'):
                    x._safe(root/'..'/'outside.json')
                target=Path(directory)/'target.json'; target.write_text('{}')
                link=root/'link.json'
                try: link.symlink_to(target)
                except OSError: self.skipTest('SYNTH symlink creation unavailable on this host')
                with self.assertRaises(ExecutionFailure): x._read_bytes(link,1024)

    def test_binary_capture_limit_is_checked_before_read(self):
        with tempfile.TemporaryDirectory(prefix='dv-plan-SYNTH-capture-') as directory:
            root=Path(directory)
            with override_settings(DV_VERIFICATION_PLAN_ROOT=root):
                target=root/'SYNTH.json'; target.write_bytes(b'12345')
                with self.assertRaisesRegex(ExecutionFailure,'capture_limit'):
                    x._read_bytes(target,4)
                self.assertEqual(x._read_bytes(target,5),b'12345')

    def test_protocol_cli_overrides_received_supervision_hooks_without_launch(self):
        from atelier.tests.test_verification_protocol import plan_fixture
        plan=plan_fixture(); batch=plan['batches'][0]; packet=p.packet_for(plan,batch)
        cli=object.__new__(x.ProtocolCLI)
        self.assertTrue(issubclass(x.ProtocolCLI,CodexCLI))
        self.assertEqual(cli.response_schema('evidence',packet),p.response_schema('evidence',packet))
        self.assertEqual(cli.prompt_for('evidence',packet),p.prompt_for('evidence',packet))
        # run() actually dispatches both hooks before spawning. The injected
        # stop at prompt generation prevents any subprocess/network operation.
        cli.identity={'model_offered_by_cli':True}; cli.timeout=10
        with tempfile.TemporaryDirectory(prefix='dv-plan-SYNTH-hook-') as directory:
            with patch.object(cli,'prompt_for',side_effect=ExecutionFailure('SYNTH_hook_reached')) as hook:
                with self.assertRaisesRegex(ExecutionFailure,'SYNTH_hook_reached'):
                    cli.run('evidence',packet,Path(directory),lambda:None)
                hook.assert_called_once_with('evidence',packet)
