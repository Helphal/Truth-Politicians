"""Shared TEST scenarios; optional wire evidence contains synthetic data only."""
import json,os,tempfile
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
from django.test import override_settings
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_counter_review as prior, test_catalogue_transfer as pagination
from atelier.models import ReceiverCursor,Source
from atelier.services.publication import packet_for_event,authorize_and_queue,evaluate_revision
from atelier.services.receiver import apply_packet
from atelier.services.receipts import receipt_for_packet,verify_receipt
from atelier.services.revisions import create_revision,record_controls,deterministic_control_results
from atelier.services.catalogue_transfer import build_catalogue_transfer,receive_catalogue_page
from atelier.services.wordpress import WordPressUnavailable
from atelier.services.exporter import export_outbox

@override_settings(DV_ENV='test',DV_NETWORK_MODE='disabled',DV_EVENT_KEY_ID='test-v1',DV_RECEIPT_KEY_ID='test-v1',
    DV_REVIEW_ATTESTATION_KEY='synthetic-counter-review-not-a-secret',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CoordinationTests(AtelierTestCase):
    make_packet=prior.CounterReviewBoundaryTests.make_packet
    configured_send=prior.CounterReviewBoundaryTests.configured_send
    resign=staticmethod(prior.CounterReviewBoundaryTests.resign)
    make_catalogue_revision=pagination.CatalogueTransferTests.make_catalogue_revision
    transfer=pagination.CatalogueTransferTests.transfer
    def emit(self,identifier,expected,**vectors):
        target=os.environ.get('DV_COORDINATION_OUTPUT')
        if target:
            from scripts.evidence_bytes import write_evidence
            path=Path(target);path.mkdir(parents=True,exist_ok=True)
            write_evidence(path/(identifier+'.json'),json.dumps({'scenario':identifier,'scope':'synthetic TEST only',
                'producer_assertions_passed':True,'expected':expected,'site_adopted':False,'vectors':vectors},ensure_ascii=False,indent=2))
    def next_revision(self,d,r,payload):
        revised=create_revision(dossier=d,public_payload=payload,source_snapshot=r.source_snapshot,
            title=r.title,short_answer=r.short_answer,main_caveat=r.main_caveat,reasoning=r.reasoning)
        record_controls(revised,deterministic_control_results(revised),environment='test')
        return revised
    def test_E01_stale_review_authority(self):
        d,r,doc,review,event,packet=self.make_packet(speech=True)
        review.revoked=True;review.save()
        with self.assertRaises(ValidationError):packet_for_event(event)
        self.emit('E01','producer rejects revoked review; frozen packet alone is not current authority',packet=packet)
    def test_E02_substantial_dependency(self):
        d,r,doc,review,event,packet=self.make_packet()
        doc.source.rights_status=Source.RightsStatus.UNKNOWN;doc.source.save()
        self.assertFalse(evaluate_revision(r,'test').allowed)
        with self.assertRaises(ValidationError):packet_for_event(event)
        self.emit('E02','producer refuses after source rights dependency changes',packet=packet,mutation={'rights_status':'unknown'})
    def test_E03_theme_replacement(self):
        payload=self.payload('official_vote')
        payload['themes']=[{'id':'THEME:A','label':'Fictif A','target_type':'dossier','target_id':'TEST:DOSSIER','origin':'proposed'}]
        d,r,_=self.make_revision(payload=payload)
        a=packet_for_event(authorize_and_queue(r,'test'));self.assertEqual(apply_packet(a),'applied_locally')
        changed=deepcopy(payload);changed['themes'][0].update(id='THEME:B',label='Fictif B')
        r2=self.next_revision(d,r,changed);b=packet_for_event(authorize_and_queue(r2,'test'))
        self.assertEqual(apply_packet(b),'applied_locally')
        self.assertEqual(ReceiverCursor.objects.get(record_id=d.stable_id).active_payload['public']['themes'],changed['themes'])
        self.emit('E03','replace entire current theme projection; no THEME:A remains',events=[a,b])
    def test_E04_transient_retry_same_event(self):
        *_,event,packet=self.make_packet()
        with self.assertRaises(WordPressUnavailable):
            self.configured_send(packet,lambda *a,**k:(_ for _ in ()).throw(WordPressUnavailable('transient_fixture')))
        receipt,calls=self.configured_send(packet,lambda p,**k:receipt_for_packet(p,'applied'))
        self.assertEqual(calls,1);self.assertTrue(verify_receipt(receipt,packet))
        self.emit('E04','retry identical event after transient failure; bound receipt',attempts=[packet,packet],receipt=receipt)
    def test_E05_relative_date(self):
        *_,packet=self.make_packet();invalid=deepcopy(packet);invalid['expires_at']='tomorrow';self.resign(invalid)
        result=apply_packet(invalid);self.assertNotEqual(result,'applied_locally')
        self.emit('E05','relative date refused',packet=invalid,observed=result)
    def test_E06_test_dependency_production(self):
        *_,packet=self.make_packet()
        with override_settings(DV_ENV='production'):result=apply_packet(packet)
        self.assertEqual(result,'wrong_destination_environment')
        self.emit('E06','TEST payload never accepted as production',packet=packet,observed=result)
    def test_E07_nominative_pagination(self):
        old,_=self.transfer();manifest,pages=build_catalogue_transfer(old['scope_scrutiny_ids'],generation=1)
        self.assertGreater(sum(x['family']=='vote_observations' for p in pages for x in p['objects']),250)
        self.assertTrue(all(len(p['objects'])<=250 for p in pages))
        for page in reversed(pages):result=receive_catalogue_page(manifest,page)
        self.assertTrue(result['complete_locally']);self.assertFalse(result['site_activated'])
        self.emit('E07','assemble whole snapshot, no silent truncation; candidate generation 1',manifest=manifest,pages=pages)
    def test_E08_suspension_priority(self):
        self.make_packet();d,r,doc,review,event,packet=self.make_packet(speech=True)
        review.revoked=True;review.save()
        with tempfile.TemporaryDirectory() as tmp:
            manifest=export_outbox(Path(tmp)/'batch',environment='test',limit=1)
        self.assertEqual(manifest['items'][0]['operation'],'suspend')
        self.emit('E08','suspension precedes unrelated upsert',manifest=manifest)
    def test_E09_bound_receipt(self):
        *_,packet=self.make_packet();receipt=receipt_for_packet(packet,'applied')
        self.assertTrue(verify_receipt(receipt,packet))
        wrong=deepcopy(receipt);wrong['event_id']='00000000-0000-0000-0000-000000000000'
        self.assertFalse(verify_receipt(wrong,packet))
        self.emit('E09','bind all identity and digest fields; applied is not published',packet=packet,receipt=receipt,invalid_receipt=wrong)
    def test_E10_restoration_requires_reconciliation(self):
        *_,packet=self.make_packet()
        with tempfile.TemporaryDirectory() as tmp,override_settings(RUNTIME_DIR=Path(tmp)):
            (Path(tmp)/'.dv-restored-unreconciled').touch()
            with self.assertRaisesMessage(WordPressUnavailable,'restored_database_requires_receiver_reconciliation'):
                self.configured_send(packet,lambda *a,**k:self.fail('no transport'))
        self.emit('E10','restored marker blocks transport even with configured reconciliation boolean',packet=packet)
