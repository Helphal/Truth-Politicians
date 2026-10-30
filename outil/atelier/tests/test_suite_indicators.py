import base64
from copy import deepcopy
from datetime import timedelta
from django.test import override_settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.tests.support import AtelierTestCase
from atelier.services.indicators import contribution,aggregate,save_contribution,prepare_aggregate,export_candidate
from atelier.services.full_review import build_review_bundle,approval_bytes
from atelier.services.canonical import sha256_digest
from atelier.services.detached_review import accept_detached_review
from atelier.services.publication import evaluate_revision,record_human_review,authorize_and_queue
from atelier.services.editorial_feed import FORMAT,DOMAIN,accept_confirmation,candidate_edition

def entry(case='case-1',**changes):
    value=dict(case_id=case,actor_id='TEST:person',actor_type='person',declaration_id='TEST:declaration',act_id='TEST:act',
        institution='Assemblée nationale',period_start='2024-01-01',period_end='2024-01-01',source_revision=1,
        qualification='convergence',vote_category='against',identity_resolved=True,nominative_available=True,
        admissible=True,zero_evidence=[],repetition_key=case,sources=[{'source_id':'TEST','version_id':'v1','sha256':'a'*64,'public_url':'https://www.assemblee-nationale.fr/test'}])
    result={**value,**changes}
    if isinstance(result['zero_evidence'],str):result['zero_evidence']=[{'source':result['sources'][0],'locator':result['zero_evidence']}]
    return result

SCOPE=dict(corpus_version='TEST-CORPUS-1',period_start='2024-01-01',period_end='2024-12-31')

class IndicatorTests(AtelierTestCase):
    def test_plus_two_distinct_zeros_minus_and_unknown(self):
        inputs=[entry('plus'),entry('zero-a',qualification='abstention',vote_category='abstention',zero_evidence='official row'),
            entry('zero-n',qualification='non_participation',vote_category='non_voting',zero_evidence='official row'),
            entry('minus',qualification='divergence',vote_category='for'),entry('unknown',vote_category='unknown')]
        result=aggregate([contribution(v) for v in inputs],**SCOPE)
        self.assertEqual(result['denominator'],4);self.assertEqual(result['counts'],{'+1':1,'0':2,'-1':1})
        self.assertEqual(len(result['excluded']),1);self.assertEqual(result['value'],{'numerator':0,'denominator':1})
        self.assertEqual(result['zero_subtypes'],{'abstention':1,'non_participation':1})
    def test_nothing_is_not_numeric_zero(self):
        self.assertIsNone(aggregate([],**SCOPE)['value'])
        zeros=[contribution(entry(str(i),qualification='abstention',vote_category='abstention',zero_evidence='row')) for i in range(2)]
        result=aggregate(zeros,**SCOPE);self.assertEqual(result['denominator'],2);self.assertEqual(result['value']['numerator'],0)
    def test_no_mechanical_vote_mapping_missing_vote_or_group_rule(self):
        self.assertEqual(contribution(entry(vote_category='against'))['value'],1)
        self.assertEqual(contribution(entry(qualification='divergence',vote_category='for'))['value'],-1)
        for changes in ({'identity_resolved':False},{'nominative_available':False},{'actor_type':'group'},{'qualification':'mixed'},
                        {'qualification':'evolution'},{'qualification':'group_divided'}, {'qualification':'non_participation','vote_category':'non_voting'}):
            with self.subTest(changes=changes):self.assertIsNone(contribution(entry(**changes))['value'])
    def test_double_version_and_repeated_declaration_excluded(self):
        a=contribution(entry());b=contribution(entry(source_revision=2));c=contribution(entry('second',repetition_key='case-1'))
        result=aggregate([a,b,c],**SCOPE);self.assertEqual(result['denominator'],0);self.assertEqual(len(result['excluded']),3)
    def test_forged_value_and_period_refused(self):
        c=contribution(entry());c['value']=-1
        with self.assertRaises(ValidationError):aggregate([c],**SCOPE)
        with self.assertRaises(ValidationError):contribution(entry(period_end='2023-01-01'))
    def signed(self,with_indicator=True):
        payload=self.payload('speech_vote');payload['result']='convergence'
        d,r,_=self.make_revision('speech_vote',payload=payload);u=self.make_user()
        item=save_contribution(r,entry(d.stable_id,sources=r.source_snapshot)) if with_indicator else None
        bundle=build_review_bundle(r);key=Ed25519PrivateKey.generate()
        keys={'test-offline':base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        statement={'algorithm':'ed25519','key_id':'test-offline','bundle_sha256':sha256_digest(bundle),'record_id':d.stable_id,'record_revision':1,'decision':'approve','reviewed_at':timezone.now().isoformat()}
        proof={'statement':statement,'signature':base64.b64encode(key.sign(approval_bytes(bundle,statement))).decode()}
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=str(u.dv_profile.internal_uid),
        ):
            accept_detached_review(dossier_id=d.pk,expected_revision=1,bundle=bundle,proof=proof,user=u,environment='test',destination='dv-site:test')
        return d,r,u,item,keys
    def test_old_signature_does_not_cover_added_indicator(self):
        d,r,u,item,keys=self.signed(False)
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            self.assertTrue(evaluate_revision(r,'test').allowed)
            save_contribution(r,entry(d.stable_id,sources=r.source_snapshot))
            self.assertFalse(evaluate_revision(r,'test').allowed)
    def test_aggregate_refuses_unsigned_and_revocation_invalidates_cache(self):
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            value=prepare_aggregate([item],**SCOPE);self.assertFalse(export_candidate(value)['publication_authorized'])
            record_human_review(dossier_id=d.pk,expected_revision=1,expected_digest=r.snapshot_digest,user=u,decision='request_changes',environment='test')
            with self.assertRaises(ValidationError):export_candidate(value)
            with self.assertRaises(ValidationError):prepare_aggregate([item],**SCOPE)
        value.refresh_from_db();self.assertFalse(value.valid)

class EditorialFeedTests(AtelierTestCase):
    signed=IndicatorTests.signed
    def period(self):return dict(period_start=timezone.now()-timedelta(days=7),period_end=timezone.now()+timedelta(hours=1))
    def confirmation(self,event):
        key=Ed25519PrivateKey.generate();keys={'test-site':base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        s=dict(format=FORMAT,event_id=str(event.event_id),event_digest=event.payload_digest,record_id=event.dossier.stable_id,
            revision=event.content_revision,environment='test',destination='dv-site:test',status='published',published_at=timezone.now().isoformat(),
            public_url='https://example.org/test-fixture',change_kind='initial',key_id='test-site')
        return s,base64.b64encode(key.sign(DOMAIN+__import__('atelier.services.canonical',fromlist=['canonical_bytes']).canonical_bytes(s))).decode(),keys
    def test_empty_collected_draft_approved_and_sent_are_not_publication(self):
        period=self.period();self.make_document();self.make_revision('speech_vote')
        self.assertEqual(candidate_edition(**period)['items'],[])
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');result=candidate_edition(**period)
        self.assertFalse(result['sendable']);self.assertFalse(result['content_available'])
    def test_confirmed_duplicate_replay_and_suspension_before_edition(self):
        d,r,u,item,keys=self.signed();period=self.period()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');s,sig,sitekeys=self.confirmation(event)
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=sitekeys):
                a=accept_confirmation(event,s,sig);b=accept_confirmation(event,s,sig);self.assertEqual(a.pk,b.pk)
                edition=candidate_edition(**period);self.assertEqual(len(edition['items']),1)
                self.assertEqual(edition,candidate_edition(**period));self.assertFalse(edition['sendable'])
                self.assertEqual(set(edition['items'][0]),{'record_id','published_revision','confirmed_at','change_kind','public_url','title','summary','main_caveat','references'})
                d.suspended=True;d.save(update_fields=['suspended'])
                self.assertEqual(candidate_edition(**period)['items'],[])
                with self.assertRaises(ValidationError):accept_confirmation(event,s,sig)
    def test_transport_receipt_wrong_signature_and_real_scope_rejected(self):
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');s,sig,sitekeys=self.confirmation(event)
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=sitekeys):
                with self.assertRaises(ValidationError):accept_confirmation(event,{'status':'applied'},sig)
                modified=deepcopy(s);modified['public_url']='https://example.org/changed'
                with self.assertRaises(ValidationError):accept_confirmation(event,modified,sig)
                d.data_scope='real';d.save(update_fields=['data_scope'])
                with self.assertRaises(ValidationError):accept_confirmation(event,s,sig)
