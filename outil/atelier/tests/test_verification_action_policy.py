from copy import deepcopy
import os
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from atelier.models import EditorialRevision, VerificationAttempt, VerificationReceipt, HumanReview, VerificationDelivery
from atelier.services import verification as v
from atelier.services.canonical import sha256_digest
from atelier.services.publication import evaluate_revision
from atelier.services.verification_export import build_site_packet
from atelier.tests import test_verification as fixture
from atelier.tests import test_verification_counterreview_v2 as export_fixture
from atelier.tests.support import AtelierTestCase


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ActionPolicyTests(TestCase):
    setUp = fixture.VerificationTests.setUp
    begin = fixture.VerificationTests.begin
    findings = fixture.VerificationTests.findings
    result = fixture.VerificationTests.result
    complete = fixture.VerificationTests.complete

    def test_all_required_actions_block_including_structural_metadata(self):
        for action in ('corriger','rechercher','suspendre','nuancer','attribuer'):
            for location in ('/title','/source_snapshot/0/source_id'):
                with self.subTest(action=action,location=location):
                    result=self.result();claim=next(c for c in result['claims'] if c['location']==location)
                    claim['action']=action
                    self.assertFalse(v._check_result(self.job,result))

    def test_present_nuance_retains_label_and_exact_text_witness(self):
        for action in ('nuancer','attribuer'):
            result=self.result();claim=next(c for c in result['claims'] if c['location']=='/title')
            claim['action']=action
            claim['action_resolution']={'status':'already_present','location':'/title','quote':'fait limité',
                                        'reason':'La restriction est présente dans le texte exact SYNTH.'}
            self.assertTrue(v._check_result(self.job,result))
            for field,value in [('quote','absent'),('location','/short_answer'),('reason','')]:
                bad=deepcopy(result);bad['claims'][result['claims'].index(claim)]['action_resolution'][field]=value
                with self.assertRaises(ValidationError):v._check_result(self.job,bad)
            claim['action']='corriger'
            with self.assertRaises(ValidationError):v._check_result(self.job,result)

    def test_required_action_persists_negative_receipt_and_blocks_gate(self):
        a=self.begin();v.record_evidence(a.pk,user=self.user,findings=self.findings())
        result=self.result();result['claims'][0]['action']='suspendre'
        receipt=v.finish(a.pk,user=self.user,result=result)
        self.assertEqual(receipt.decision,'not_supported');self.assertFalse(v.gate(self.revision)[0])
        self.assertFalse(evaluate_revision(self.revision,'test').allowed)
        self.assertEqual(v.finish(a.pk,user=self.user,result=result).pk,receipt.pk)
        self.assertEqual(HumanReview.objects.count(),0)

    def test_gate_and_idempotent_finish_recheck_inconsistent_action(self):
        receipt=self.complete();a=receipt.attempt
        altered=deepcopy(a.result);altered['claims'][0]['action']='corriger';digest=sha256_digest(altered)
        # Counterexample represents corrupt/admin-imported rows, not an allowed writer API.
        VerificationAttempt.objects.filter(pk=a.pk).update(result=altered,result_digest=digest)
        VerificationReceipt.objects.filter(pk=receipt.pk).update(result_digest=digest)
        self.assertFalse(v.gate(self.revision)[0])
        with self.assertRaises(ValidationError):v.finish(a.pk,user=self.user,result=altered)

    def legacy(self):
        with patch.object(v,'POLICY',v.LEGACY_POLICY):
            self.job=v.enqueue(self.revision);old=self.complete()
        return old

    def test_legacy_receipt_requires_explicit_traced_revalidation(self):
        old=self.legacy();old_result=deepcopy(old.attempt.result);old_hash=old.result_digest
        self.assertFalse(v.gate(self.revision)[0])
        new=v.revalidate_legacy_receipt(old.pk,user=self.user)
        self.assertNotEqual(new.pk,old.pk);self.assertTrue(v.gate(self.revision)[0])
        self.assertEqual(new.job.policy,v.POLICY)
        self.assertIn('no new semantic model invocation',new.attempt.result['limitations'][-1])
        old.refresh_from_db();self.assertEqual(old.result_digest,old_hash);self.assertEqual(old.attempt.result,old_result)
        self.assertEqual(v.revalidate_legacy_receipt(old.pk,user=self.user).pk,new.pk)
        self.assertEqual(HumanReview.objects.count(),0)

    def test_legacy_revalidation_rejects_changed_source_and_unresolved_action(self):
        old=self.legacy();self.passage.context='Changed source context';self.passage.save()
        with self.assertRaisesMessage(ValidationError,'legacy_sources_or_revision_changed'):
            v.revalidate_legacy_receipt(old.pk,user=self.user)
        self.passage.context='Source fictive réservée au banc.';self.passage.save()
        a=old.attempt;bad=deepcopy(a.result);bad['claims'][0]['action']='rechercher';digest=sha256_digest(bad)
        VerificationAttempt.objects.filter(pk=a.pk).update(result=bad,result_digest=digest)
        VerificationReceipt.objects.filter(pk=old.pk).update(result_digest=digest)
        with self.assertRaisesMessage(ValidationError,'legacy_actions_require_new_semantic_pass'):
            v.revalidate_legacy_receipt(old.pk,user=self.user)

    def test_unapplied_nuance_needs_new_revision_and_old_replay_fails(self):
        a=self.begin();v.record_evidence(a.pk,user=self.user,findings=self.findings())
        old_result=self.result();old_result['claims'][0]['action']='nuancer'
        self.assertEqual(v.finish(a.pk,user=self.user,result=old_result).decision,'not_supported')
        revised=EditorialRevision.objects.create(dossier=self.dossier,revision=2,title='SYNTH fait limité. Avec réserve.',
            public_payload=self.revision.public_payload,snapshot_digest=sha256_digest('new'),
            verification_required=True,source_snapshot=self.revision.source_snapshot,created_by=self.editor)
        self.dossier.current_revision=2;self.dossier.save();self.revision=revised;self.job=v.enqueue(revised)
        with self.assertRaises(ValidationError):v.finish(a.pk,user=self.user,result=old_result)
        self.assertFalse(v.gate(revised)[0]);new=self.complete();self.assertEqual(new.decision,'supported')
        self.assertFalse(evaluate_revision(revised,'test').allowed)

    def test_interrupted_old_attempt_cannot_close_new_one(self):
        a=self.begin();v.interrupt(a.pk,user=self.user);b=self.begin()
        self.assertNotEqual(a.pk,b.pk)
        with self.assertRaises(ValidationError):v.finish(a.pk,user=self.user,result=self.result())
        v.record_evidence(b.pk,user=self.user,findings=self.findings())
        bad=self.result();bad['claims'][0]['action']='rechercher'
        self.assertEqual(v.finish(b.pk,user=self.user,result=bad).decision,'not_supported')

    def test_queue_current_versions_separate_from_history(self):
        self.client.force_login(self.user)
        old=self.job
        self.source.access_status='blocked';self.source.save();new=v.enqueue(self.revision)
        response=self.client.get('/verification/')
        self.assertEqual([x.pk for x in response.context['page']], [new.pk])
        response=self.client.get('/verification/?history=1')
        self.assertEqual({x.pk for x in response.context['page']},{old.pk,new.pk})


class ActionExportTests(AtelierTestCase):
    prepare=export_fixture.ExportCounterReviewTests.prepare

    def test_signed_export_policy_and_no_review_bypass(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        with self.final_authority_settings(reviewer),patch.dict(os.environ,{
            'DV_SITE_VERIFICATION_KEY_ID':'synth-test','DV_SITE_VERIFICATION_KEY':'PUBLIC-SYNTH-TEST-VECTOR-NOT-A-SECRET'}):
            with self.assertRaises(ValidationError):build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            self.approve(dossier,revision,reviewer)
            packet=build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            self.assertEqual(packet['verification']['policy'],v.POLICY)
            receipt=VerificationReceipt.objects.get(job__revision=revision);a=receipt.attempt
            bad=deepcopy(a.result);bad['claims'][0]['action']='suspendre';digest=sha256_digest(bad)
            VerificationAttempt.objects.filter(pk=a.pk).update(result=bad,result_digest=digest)
            VerificationReceipt.objects.filter(pk=receipt.pk).update(result_digest=digest)
            with self.assertRaises(ValidationError):build_site_packet(revision,user=verifier,environment='test',sequence=2,mapping=mapping)
            self.assertEqual(VerificationDelivery.objects.count(),1)
