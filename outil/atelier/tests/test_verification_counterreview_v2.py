"""Contre-revue integration, uniquement donnees Django SYNTH de test."""
from copy import deepcopy
import os
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from atelier.models import EvidencePassage, EditorialRevision, VerificationReceipt
from atelier.services import verification as v
from atelier.services.verification_export import build_site_packet
from atelier.services.canonical import sha256_digest
from atelier.tests import test_verification as fixture
from atelier.tests.support import AtelierTestCase


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class QueueCounterReviewTests(TestCase):
    setUp = fixture.VerificationTests.setUp
    begin = fixture.VerificationTests.begin
    findings = fixture.VerificationTests.findings
    result = fixture.VerificationTests.result
    complete = fixture.VerificationTests.complete

    def test_restored_source_allows_new_examination_without_rewriting_content(self):
        original_receipt=self.complete()
        self.source.access_status='blocked'; self.source.save()
        v.enqueue(self.revision)
        self.source.access_status='active'; self.source.save()
        again=v.enqueue(self.revision)
        self.assertNotEqual(again.state,'stale','Une source revenue ne doit pas laisser la file irrecuperable.')
        self.assertNotEqual(again.pk,self.job.pk)
        self.assertFalse(v.gate(self.revision)[0])
        self.job=again
        new_receipt=self.complete()
        self.assertNotEqual(new_receipt.pk,original_receipt.pk)
        self.assertEqual(VerificationReceipt.objects.count(),2)
        self.assertTrue(v.gate(self.revision)[0])

    def test_enqueue_old_revision_does_not_stale_current_job(self):
        old=self.revision
        updated={**old.public_payload,'title':'SYNTH revision deux'}
        current=EditorialRevision.objects.create(dossier=self.dossier,revision=2,
            title=updated['title'],public_payload=updated,snapshot_digest=sha256_digest(updated),
            verification_required=True,source_snapshot=old.source_snapshot,created_by=self.editor)
        self.dossier.current_revision=2;self.dossier.save()
        current_job=v.enqueue(current)
        with self.assertRaises(ValidationError):v.enqueue(old)
        current_job.refresh_from_db()
        self.assertEqual(current_job.state,'pending')

    def test_structural_metadata_does_not_discard_declared_counterevidence(self):
        attempt=self.begin()
        v.record_evidence(attempt.pk,user=self.user,findings=self.findings())
        result=self.result()
        claim=next(c for c in result['claims'] if c['location']=='/source_snapshot/0/source_id')
        claim.update(nature='metadata',verdict='NON_VERIFIABLE_COMME_FAIT',
            against=[{'passage_id':self.passage.pk,'quote':self.passage.exact_text}],
            reason='Identite de source contradictoire non resolue.',action='rechercher')
        receipt=v.finish(attempt.pk,user=self.user,result=result)
        self.assertEqual(receipt.decision,'not_supported')


class ExportCounterReviewTests(AtelierTestCase):
    def prepare(self):
        payload=self.payload('speech_vote')
        payload['attribution']={'person_id':'SYN:PERSON','organization_id':None,
            'speaker_label':'Acteur TEST','capacity':'personnelle'}
        dossier,revision,doc=self.make_revision('speech_vote',payload=payload)
        verifier=self.make_user('verifier'); reviewer=self.make_user('reviewer')
        revision.created_by=self.make_user('editor');revision.verification_required=True;revision.save()
        passage=EvidencePassage.objects.create(document=doc,exact_text='SYNTH preuve integration.',
            locator='ligne 1',public_url=doc.source.document_url)
        job=v.enqueue(revision)
        attempt=v.start(job.pk,user=verifier,execution_id='synthetic:mapping-test',provider='synthetic')
        v.record_evidence(attempt.pk,user=verifier,findings={
            'sources':[{'source_id':doc.source.stable_id,'version_id':doc.version_id,
                'finding':'Preuve SYNTH.', 'locators':['ligne 1'],'limits':[]}],
            'counter_search':['Contradiction SYNTH recherchee.'],'limitations':['SYNTH seulement.']})
        result={'contract':v.POLICY,'fingerprint':job.fingerprint,'coverage_statement':'Tout SYNTH.',
            'limitations':['Test seulement.'],'claims':[{**c,'nature':'metadata','verdict':'ETAYE_DANS_PERIMETRE',
            'support':[{'passage_id':passage.pk,'quote':passage.exact_text}],'against':[],
            'limits':[],'reason':'Vecteur fictif.','action':'conserver'} for c in job.input_snapshot['claims']]}
        v.finish(attempt.pk,user=verifier,result=result)
        mapping={'sources':{doc.source.stable_id:{'source_id':'SYNTH-SITE-SOURCE',
            'version_id':'source-v1','sha256':doc.sha256}},'person_external_id':'','related_scrutin_external_id':''}
        return dossier,revision,verifier,reviewer,mapping

    def test_unverified_actor_mapping_cannot_replace_reviewed_identity(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        mapping['person_external_id']='SYNTH-UNRELATED-PERSON'
        with self.final_authority_settings(reviewer), patch.dict(os.environ,{
            'DV_SITE_VERIFICATION_KEY_ID':'synth-test',
            'DV_SITE_VERIFICATION_KEY':'PUBLIC-SYNTH-TEST-VECTOR-NOT-A-SECRET'}):
            self.approve(dossier,revision,reviewer)
            with self.assertRaises(ValidationError):
                build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)

    def test_unverified_scrutin_mapping_cannot_replace_reviewed_vote(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        mapping['related_scrutin_external_id']='SYNTH-UNRELATED-SCRUTIN'
        with self.final_authority_settings(reviewer), patch.dict(os.environ,{
            'DV_SITE_VERIFICATION_KEY_ID':'synth-test',
            'DV_SITE_VERIFICATION_KEY':'PUBLIC-SYNTH-TEST-VECTOR-NOT-A-SECRET'}):
            self.approve(dossier,revision,reviewer)
            with self.assertRaises(ValidationError):
                build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
