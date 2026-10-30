"""SYNTH only: durable file races, legacy replay, leases and transport wiring."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from atelier.services import verification_plan_executor as x
from atelier.services import verification_transport as wire
from atelier.services import verification as v
from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.services.verification_executor import ExecutionFailure
from atelier.models import UserProfile, VerificationAttempt, VerificationReceipt
from atelier.tests import test_verification_plan_executor as base


class AtomicFileTests(SimpleTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='dv-draft-SYNTH-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        override=override_settings(DV_VERIFICATION_PLAN_ROOT=self.root)
        override.enable();self.addCleanup(override.disable)
        self.a={'plan_digest':sha256_digest('SYNTH-planA'),'owner_id':1,
                'source_read_identity':sha256_digest('SYNTH-sources')}
        self.b={'plan_digest':sha256_digest('SYNTH-planB'),'owner_id':1,
                'source_read_identity':sha256_digest('SYNTH-other-sources')}
        self.allocation={'contract':x.ALLOCATION,'allocation_id':'SYNTH:atomic-214',
            'authorization':'explicit_dv_verite','authority_reference':'SYNTH, no real authority',
            'owner_id':1,'plan_digests':[self.a['plan_digest'],self.b['plan_digest']],
            'fingerprints':[sha256_digest('SYNTH-A'),sha256_digest('SYNTH-B')],
            'job_ids':[1001,1002],'model':'SYNTH','effort':'none','max_phases':214,
            'expires_at':(timezone.now()+timedelta(hours=1)).isoformat()}
        self.binding={'input_sha256':'a'*64,'logical_input_sha256':'a'*64,
                      'logical_packet_sha256':'b'*64,'transport_mode':'legacy'}

    def reserve(self,plan,label):
        return x._reserve_phase(self.allocation,plan,{'batch_id':label},1,self.binding,1)[0]

    def seed(self,count):
        _,path,ledger=x._ledger(self.allocation)
        for n in range(count):
            batch='SYNTH-seeded-'+str(n)
            ledger['phases'].append({'phase_id':sha256_digest([self.allocation['allocation_id'],self.a['plan_digest'],batch,1]),
                'plan_digest':self.a['plan_digest'],'batch_id':batch,'run_number':1,'owner_id':1,
                'attempt_id':1,'allocation_id':self.allocation['allocation_id'],'input_sha256':'a'*64,
                'state':'accepted','translated_sha256':'c'*64})
        x._write(path,ledger)
        return deepcopy(ledger['phases'])

    def test_two_contenders_at_213_charge_only_one(self):
        prefix=self.seed(213);barrier=threading.Barrier(2)
        def contend(plan,label):
            barrier.wait(timeout=3)
            try:return ('reserved',self.reserve(plan,label))
            except ExecutionFailure as error:return ('refused',error.code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(contend,p,l) for p,l in ((self.a,'SYNTH-A'),(self.b,'SYNTH-B'))]
            results=[f.result(timeout=8) for f in futures]
        ledger=x._allocation_snapshot(self.allocation)
        self.assertEqual(len(ledger['phases']),214)
        self.assertEqual(ledger['phases'][:213],prefix)
        self.assertEqual(sorted(r[0] for r in results),['refused','reserved'])
        self.assertIn(('refused','allocation_ceiling_reached'),results)

    def test_reload_ack_retains_other_plan_and_exact_legacy_prefix(self):
        old=self.seed(1)[0]
        stale=x._allocation_snapshot(self.allocation)
        other=self.reserve(self.b,'SYNTH-other')
        x._ack_phase(self.allocation,other['phase_id'],{'state':'launched'})
        x._ack_phase(self.allocation,other['phase_id'],{'state':'returned','raw_sha256':'d'*64})
        x._ack_phase(self.allocation,old['phase_id'],{'state':'accepted','translated_sha256':'c'*64,'mapping_sha256':'e'*64})
        ledger=x._allocation_snapshot(self.allocation)
        self.assertEqual(ledger['phases'][0],old)
        self.assertNotIn('mapping_sha256',ledger['phases'][0])
        self.assertEqual(len(ledger['phases']),len(stale['phases'])+1)
        self.assertEqual(ledger['phases'][1]['raw_sha256'],'d'*64)

    def test_provider_work_does_not_hold_allocation_lock(self):
        barrier=threading.Barrier(2)
        def simulated(plan,label):
            with x._lock(x._state_directory(plan)/'lock'):
                phase=self.reserve(plan,label)
                x._ack_phase(self.allocation,phase['phase_id'],{'state':'launched'})
                barrier.wait(timeout=3)  # both provider stand-ins overlap here
                x._ack_phase(self.allocation,phase['phase_id'],{'state':'returned','raw_sha256':'d'*64})
                return phase['phase_id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(simulated,p,l) for p,l in ((self.a,'SYNTH-A'),(self.b,'SYNTH-B'))]
            self.assertEqual(len({f.result(timeout=8) for f in futures}),2)
        self.assertEqual(len(x._allocation_snapshot(self.allocation)['phases']),2)

    def test_reservation_crash_is_not_refunded_or_reserved_twice(self):
        phase=self.reserve(self.a,'SYNTH-crash-before-journal')
        with self.assertRaisesRegex(ExecutionFailure,'allocation_phase_already_reserved'):
            self.reserve(self.a,'SYNTH-crash-before-journal')
        ledger=x._allocation_snapshot(self.allocation)
        self.assertEqual(len(ledger['phases']),1)
        self.assertEqual(ledger['phases'][0]['phase_id'],phase['phase_id'])

    def test_recovery_preserves_other_plan_after_reserved_crash(self):
        phase=self.reserve(self.a,'SYNTH-reserved');other=self.reserve(self.b,'SYNTH-live')
        x._ack_phase(self.allocation,other['phase_id'],{'state':'launched'})
        journal={'batches':{}}
        x._recover_allocation(self.a,journal,self.allocation)
        after=x._allocation_snapshot(self.allocation)
        self.assertEqual(len(after['phases']),2)
        self.assertEqual(after['phases'][1]['state'],'launched')
        self.assertEqual(journal['batches']['SYNTH-reserved']['error_code'],'reserved_before_launch')
        self.assertEqual(after['phases'][0]['phase_id'],phase['phase_id'])

    def test_expensive_recovery_does_not_block_peer_ack_or_rewrite_eight_terminals(self):
        self.seed(8)
        _,path,ledger=x._ledger(self.allocation)
        translated={'SYNTH':'translated'};mapping={'SYNTH':'mapping'};raw={'SYNTH':'raw'}
        raw_bytes=canonical_bytes(raw);raw_sha=hashlib.sha256(raw_bytes).hexdigest()
        journal={'batches':{}};plan={**self.a,'limits':{'max_output_bytes':4096},'batches':[]}
        for phase in ledger['phases']:
            phase['translated_sha256']=sha256_digest(translated);phase['raw_sha256']=raw_sha
            state={**phase,'mapping_sha256':sha256_digest(mapping)}
            journal['batches'][phase['batch_id']]=state
            plan['batches'].append({'batch_id':phase['batch_id'],'stage':'claims'})
            directory=x._state_directory(plan)/phase['batch_id']/'1'
            for name,value in (('raw-return.json',raw),('translated.json',translated),('mapping.json',mapping),
                ('return-receipt.json',{'phase_id':phase['phase_id'],'plan_digest':plan['plan_digest'],
                    'input_sha256':phase['input_sha256'],'raw_sha256':raw_sha})):
                x._write(directory/name,value)
        x._write(path,ledger);prefix=deepcopy(ledger['phases'])
        peer=self.reserve(self.b,'SYNTH-peer-already-launched')
        x._ack_phase(self.allocation,peer['phase_id'],{'state':'launched'})
        entered=threading.Event();release=threading.Event();once=[False]
        def crypto_stand_in(*args):
            if not once[0]:
                once[0]=True;entered.set()
                self.assertTrue(release.wait(timeout=8))
        with patch.object(x,'_validate_return_transport',side_effect=crypto_stand_in):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def recover():
                    with x._lock(x._state_directory(plan)/'lock'):
                        return x._recover_allocation(plan,journal,self.allocation)
                future=pool.submit(recover);self.assertTrue(entered.wait(timeout=2))
                begin=time.monotonic()
                x._ack_phase(self.allocation,peer['phase_id'],{'state':'returned','raw_sha256':'d'*64})
                self.assertLess(time.monotonic()-begin,1)
                # Keep recovery blocked longer than the allocation lock wait.
                time.sleep(5.1);release.set();future.result(timeout=3)
        after=x._allocation_snapshot(self.allocation)['phases']
        self.assertEqual(after[:8],prefix);self.assertEqual(after[-1]['state'],'returned')
        self.assertEqual(after[-1]['raw_sha256'],'d'*64)

    def test_returned_journal_with_lost_allocation_ack_recovers_raw_hash(self):
        self._lost_allocation_ack('returned')

    def test_accepted_journal_with_lost_allocation_acks_recovers_acceptance(self):
        self._lost_allocation_ack('accepted')

    def _lost_allocation_ack(self,journal_state):
        old=self.seed(1)[0];phase=self.reserve(self.b,'SYNTH-lost-ACK')
        x._ack_phase(self.allocation,phase['phase_id'],{'state':'launched'})
        plan={**self.b,'limits':{'max_output_bytes':4096},
              'batches':[{'batch_id':phase['batch_id'],'stage':'claims'}]}
        raw={'SYNTH':'raw'};raw_sha=hashlib.sha256(canonical_bytes(raw)).hexdigest()
        translated={'SYNTH':'translated'};mapping={'SYNTH':'mapping'}
        state={**phase,'state':journal_state,'raw_sha256':raw_sha,
            'translated_sha256':sha256_digest(translated),'mapping_sha256':sha256_digest(mapping)}
        journal={'batches':{phase['batch_id']:state}}
        directory=x._state_directory(plan)/phase['batch_id']/'1'
        for name,value in (('raw-return.json',raw),('translated.json',translated),('mapping.json',mapping),
                ('return-receipt.json',{'phase_id':phase['phase_id'],'plan_digest':plan['plan_digest'],
                    'input_sha256':phase['input_sha256'],'raw_sha256':raw_sha})):
            x._write(directory/name,value)
        with patch.object(x,'_validate_return_transport',return_value=None):
            x._recover_allocation(plan,journal,self.allocation)
        after=x._allocation_snapshot(self.allocation)['phases'];self.assertEqual(after[0],old)
        self.assertEqual(len(after),2);self.assertEqual(after[1]['state'],journal_state)
        self.assertEqual(after[1]['raw_sha256'],raw_sha)
        if journal_state=='accepted':
            self.assertEqual(after[1]['translated_sha256'],sha256_digest(translated))
            self.assertEqual(after[1]['mapping_sha256'],sha256_digest(mapping))

    def test_pending_lock_is_bounded_and_released_after_exception(self):
        entered=threading.Event();release=threading.Event();lock=self.root/'SYNTH-held-lock'
        def hold():
            with x._lock(lock):entered.set();release.wait(timeout=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(hold);self.assertTrue(entered.wait(timeout=2))
            start=time.monotonic()
            with self.assertRaisesRegex(ExecutionFailure,'allocation_already_running'):
                with x._lock(lock,wait_seconds=.05):pass
            self.assertLess(time.monotonic()-start,1)
            release.set();future.result(timeout=2)
        with x._lock(lock):pass

    def test_application_mutations_are_serialized(self):
        barrier=threading.Barrier(2);events=[]
        def write(label):
            barrier.wait(timeout=2)
            with x._application_write():
                events.append(('start',label));time.sleep(.03);events.append(('end',label))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(write,label) for label in ('A','B')]
            for future in futures:future.result(timeout=3)
        self.assertEqual(events[0][0],'start');self.assertEqual(events[1],('end',events[0][1]))
        self.assertEqual(events[2][0],'start');self.assertEqual(events[3],('end',events[2][1]))

    def test_source_receipt_exact_bytes_survive_new_lease(self):
        findings={'sources':['SYNTH-only'],'limitations':['no real source']}
        original=x._store_source_receipt(self.a,findings,owner_id=1,attempt_id=11)
        path=x._state_directory(self.a)/'source-receipt.json';before=path.read_bytes()
        replay=x._store_source_receipt(self.a,findings,owner_id=1,attempt_id=12)
        self.assertEqual(path.read_bytes(),before);self.assertEqual(replay,original)
        proof=x._read(x._state_directory(self.a)/'source-bindings/attempt-12.json')
        self.assertFalse(proof['independent_reading']);self.assertEqual(proof['original_attempt_id'],11)
        self.assertEqual(proof['source_receipt_sha256'],sha256_digest(original))

    def test_source_identity_or_findings_change_is_refused(self):
        original=x._store_source_receipt(self.a,{'SYNTH':'same'},owner_id=1,attempt_id=11)
        for plan,findings in ((self.a,{'SYNTH':'changed'}),({**self.a,'source_read_identity':'d'*64},{'SYNTH':'same'})):
            with self.assertRaisesRegex(ExecutionFailure,'immutable_source_receipt_changed'):
                x._store_source_receipt(plan,findings,owner_id=1,attempt_id=12)
        self.assertEqual(x._read(x._state_directory(self.a)/'source-receipt.json'),original)


class LeaseTests(base.PlanExecutorTests):
    def test_explicit_checkpoint_closes_then_rolls_over_without_new_prefix_calls(self):
        self.pilot_plan()
        first=base.SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(first,stop_after_accepted_batches=2,close_lease_at_checkpoint=True)
        original=self.ledger()['phases'][:];source=(x._state_directory(self.plan)/'source-receipt.json').read_bytes()
        prior=VerificationAttempt.objects.get(pk=self.journal()['attempt_ids'][-1]);expires=prior.expires_at
        self.assertEqual(prior.stage,'error');self.assertEqual(prior.error_code,'interrupted')
        self.assertTrue(self.journal()['batches']);second=base.SynthRunner(self.plan)
        receipt=self.execute(second)
        self.assertEqual(receipt.decision,'supported')
        self.assertEqual(self.ledger()['phases'][:len(original)],original)
        self.assertEqual((x._state_directory(self.plan)/'source-receipt.json').read_bytes(),source)
        prior.refresh_from_db();self.assertEqual(prior.expires_at,expires)
        self.assertEqual(len(self.journal()['attempt_ids']),2)
        self.assertTrue(all(s=='claims' for s,_ in second.calls))

    def test_accepted_replay_interleaves_peer_reservation_without_erasing_it(self):
        self.pilot_plan()
        peer={'plan_digest':sha256_digest('SYNTH-concurrent-peer'),'owner_id':self.user.pk}
        self.allocation['plan_digests'].append(peer['plan_digest'])
        self.allocation['fingerprints'].append('d'*64);self.allocation['job_ids'].append(999)
        self.allocation['max_phases']=214;x._write(self.allocation_path,self.allocation)
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(stop_after_accepted_batches=2,close_lease_at_checkpoint=True)
        prefix=deepcopy(self.ledger()['phases']);entered=threading.Event();done=threading.Event();peer_phase=[]
        def peer_work():
            self.assertTrue(entered.wait(timeout=3))
            binding={'input_sha256':'a'*64,'logical_input_sha256':'a'*64,'logical_packet_sha256':'b'*64,'transport_mode':'legacy'}
            phase,_=x._reserve_phase(self.allocation,peer,{'batch_id':'SYNTH-peer'},1,binding,1)
            x._ack_phase(self.allocation,phase['phase_id'],{'state':'launched'})
            x._ack_phase(self.allocation,phase['phase_id'],{'state':'returned','raw_sha256':'c'*64})
            peer_phase.append(phase['phase_id']);done.set()
        real=x._translate;once=[False]
        def replay(*args,**kwargs):
            if not once[0] and args[2]['state']=='accepted':
                once[0]=True;entered.set();self.assertTrue(done.wait(timeout=5))
            return real(*args,**kwargs)
        runner=base.SynthRunner(self.plan)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(peer_work)
            with patch.object(x,'_translate',side_effect=replay):
                with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
                    self.execute(runner,stop_after_accepted_batches=2,close_lease_at_checkpoint=True)
            future.result(timeout=5)
        phases=self.ledger()['phases'];self.assertEqual(phases[:len(prefix)],prefix)
        self.assertEqual(phases[-1]['phase_id'],peer_phase[0]);self.assertEqual(phases[-1]['state'],'returned')
        self.assertEqual(runner.calls,[])

    def test_short_remaining_lease_stops_before_new_reservation(self):
        self.pilot_plan()
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(stop_after_accepted_batches=2)
        attempt=VerificationAttempt.objects.get(pk=self.journal()['attempt_ids'][-1])
        expires=timezone.now()+timedelta(seconds=610)
        VerificationAttempt.objects.filter(pk=attempt.pk).update(expires_at=expires)
        spent=len(self.ledger()['phases']);runner=base.SynthRunner(self.plan)
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(runner,close_lease_at_checkpoint=True)
        self.assertEqual(runner.calls,[]);self.assertEqual(len(self.ledger()['phases']),spent)
        attempt.refresh_from_db();self.assertEqual(attempt.expires_at,expires)
        checkpoint=x._read(x._state_directory(self.plan)/'pilot-checkpoint.json')
        self.assertEqual(checkpoint['reason'],'lease_budget_before_launch');self.assertTrue(checkpoint['lease_closed'])

    def test_lossless_synth_wiring_has_actual_and_logical_sha_and_standard_receipt(self):
        receipt=self.execute(transport='lossless-v1')
        self.assertEqual(receipt.decision,'supported');self.assertEqual(VerificationReceipt.objects.count(),1)
        for batch in self.plan['batches']:
            state=self.journal()['batches'][batch['batch_id']]
            directory=x._state_directory(self.plan)/batch['batch_id']/str(state['run_number'])
            packet=x._read(directory/'packet.json');proof=wire.receipt(batch['stage'],packet)
            self.assertEqual(state['input_sha256'],proof['input_sha256'])
            self.assertEqual(state['logical_input_sha256'],proof['logical_input_sha256'])
            self.assertNotEqual(state['input_sha256'],state['logical_input_sha256'])
            self.assertEqual(state['transport_receipt_sha256'],sha256_digest(proof))
            self.assertEqual(wire.validate_transport(x._read(directory/'transport-frame.json'),packet),packet)
            self.assertEqual(x._read(directory/'return-receipt.json')['raw_origin'],'SYNTH_adapter')

    def test_real_scope_cannot_use_modified_adapter_origin(self):
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(stop_after_accepted_batches=1)
        batch=self.plan['batches'][0];state=deepcopy(self.journal()['batches'][batch['batch_id']])
        directory=x._state_directory(self.plan)/batch['batch_id']/str(state['run_number'])
        receipt=x._read(directory/'return-receipt.json')
        # Legacy entries had no identity/origin marker. This reproduces that
        # format without relying on new marker fields to reject a forged origin.
        for name in ('runner_identity_sha256','return_origin'):
            state.pop(name,None);receipt.pop(name,None)
        real_plan=deepcopy(self.plan);real_plan['snapshot']['scope']='real'
        real_plan.update(model='gpt-6-sol',effort='high')
        identity={'model':'gpt-6-sol','model_reasoning_effort':'high','model_provider':'openai'}
        receipt.update(raw_origin='provider_response_file',runner_identity=identity)
        raw=(directory/'raw-return.json').read_bytes();(directory/'response.json').write_bytes(raw)
        x._write(directory/'execution.json',{'executor':'dv.codex-executor/1','stage':batch['stage'],
            'returncode':0,'tool_events':0,'input_sha256':state['input_sha256'],
            'identity':identity,'output_sha256':hashlib.sha256(raw).hexdigest()})
        native=x._recovered_output
        with patch.object(x,'_recovered_output',wraps=native) as control:
            x._validate_return_transport(real_plan,batch['stage'],directory,state,receipt)
            control.assert_called_once()
        for origin,code in (('SYNTH_adapter','synthetic_return_outside_qualified_scope'),
                            ('unknown','unknown_provider_return_origin')):
            forged={**receipt,'raw_origin':origin}
            with self.subTest(origin=origin),self.assertRaisesRegex(ExecutionFailure,code):
                x._validate_return_transport(real_plan,batch['stage'],directory,state,forged)
            self.assertEqual(forged['input_sha256'],receipt['input_sha256'])
            self.assertEqual(forged['raw_sha256'],receipt['raw_sha256'])
        with self.assertRaisesRegex(ExecutionFailure,'persisted_runner_configuration_changed'):
            x._validate_return_transport(real_plan,batch['stage'],directory,state,
                {**receipt,'runner_identity':{**identity,'model':'different'}})

    def test_synth_recovery_requires_original_qualified_attempt_and_test_identity(self):
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(stop_after_accepted_batches=1)
        batch=self.plan['batches'][0];state=self.journal()['batches'][batch['batch_id']]
        directory=x._state_directory(self.plan)/batch['batch_id']/str(state['run_number'])
        receipt=x._read(directory/'return-receipt.json')
        x._validate_return_transport(self.plan,batch['stage'],directory,state,receipt)
        for guard in ('environment','identity','attempt_provider'):
            with self.subTest(guard=guard):
                if guard=='environment':
                    with override_settings(DV_ENV='local'),self.assertRaisesRegex(ExecutionFailure,'synthetic_return_outside_qualified_scope'):
                        x._validate_return_transport(self.plan,batch['stage'],directory,state,receipt)
                elif guard=='identity':
                    UserProfile.objects.filter(user_id=self.user.pk).update(is_test_identity=False)
                    with self.assertRaisesRegex(ExecutionFailure,'synthetic_return_outside_qualified_scope'):
                        x._validate_return_transport(self.plan,batch['stage'],directory,state,receipt)
                    UserProfile.objects.filter(user_id=self.user.pk).update(is_test_identity=True)
                else:
                    VerificationAttempt.objects.filter(pk=state['attempt_id']).update(provider='codex_session')
                    with self.assertRaisesRegex(ExecutionFailure,'synthetic_return_outside_qualified_scope'):
                        x._validate_return_transport(self.plan,batch['stage'],directory,state,receipt)

    def test_graceful_pause_waits_for_return_then_stops_without_next_reservation(self):
        self.pilot_plan();pause=self.root/'SYNTH-PAUSE'
        def request_pause(stage,packet,directory):
            pause.touch()
        runner=base.SynthRunner(self.plan,action=request_pause)
        with self.assertRaisesRegex(ExecutionFailure,'pilot_checkpoint_received'):
            self.execute(runner,pause_before_launch_file=pause,close_lease_at_checkpoint=True)
        self.assertEqual(len(runner.calls),1)
        phases=self.ledger()['phases'];self.assertEqual(len(phases),1)
        self.assertEqual(phases[0]['state'],'accepted')
        self.assertEqual(VerificationReceipt.objects.count(),0)
        checkpoint=x._read(x._state_directory(self.plan)/'pilot-checkpoint.json')
        self.assertEqual(checkpoint['reason'],'pause_before_launch');self.assertTrue(checkpoint['lease_closed'])
        self.assertNotIn(checkpoint['next_batch'],self.journal()['batches'])
        attempt=VerificationAttempt.objects.get(pk=checkpoint['attempt_id'])
        self.assertEqual(attempt.stage,'error');self.assertEqual(attempt.error_code,'interrupted')
        self.assertEqual(attempt.expires_at.isoformat(),checkpoint['expires_at_unchanged'])
        source=(x._state_directory(self.plan)/'source-receipt.json').read_bytes()
        pause.unlink();resumed=base.SynthRunner(self.plan);self.execute(resumed)
        self.assertTrue(all(stage=='claims' for stage,bid in resumed.calls))
        self.assertEqual(self.ledger()['phases'][0],phases[0])
        self.assertEqual((x._state_directory(self.plan)/'source-receipt.json').read_bytes(),source)
