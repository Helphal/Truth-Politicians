"""SYNTH: selected dissemination is distinct from the private review archive."""
from copy import deepcopy
import json
import os
from unittest.mock import patch
from django.core.exceptions import ValidationError
from atelier.models import VerificationReceipt, HumanReview, PublicationSnapshot, OutboxEvent
from atelier.services import verification as v
from atelier.services.canonical import sha256_digest
from atelier.services.diffusion import PUBLIC_CONTRACT, validate_diffusion_selection, selected_public_payload, project_verified_content
from atelier.services.contracts import validate_speech_vote_payload
from atelier.services.full_review import build_review_bundle, DIFFUSION_FORMAT
from atelier.services.publication import authorize_and_queue
from atelier.services.verification_export import build_site_packet, build_site_suspension
from atelier.tests.test_verification_counterreview_v2 import ExportCounterReviewTests

PRIVATE_MARKER='PRIVATE-WORKING-ARCHIVE-SYNTH-ONLY'

class DiffusionSelectionTests(ExportCounterReviewTests):
    def make_revision(self,kind='speech_vote',**kwargs):
        doc=kwargs.pop('document',None) or self.make_document()
        public=kwargs.pop('payload',None) or self.payload(kind)
        public['public_contract_version']=PUBLIC_CONTRACT
        public['declaration_source_url']=doc.source.document_url
        public['evidence']=[{'source_version':self.snapshot(doc)[0], 'locator':'SYNTH exact line',
            'transcription_status':'SYNTH exact', 'exact_quote':PRIVATE_MARKER+' '+('Contexte privé. '*200)+public['declaration_quote']}]
        public['diffusion_selection']={'contract':'dv.diffusion-selection/1','passages':[{
            'evidence_index':0,'exact_quote':public['declaration_quote'],'attribution':'SYNTH auteur, date, institution.'}],
            'public_note':'SYNTH sélection minimale ; archive conservée en privé.'}
        validate_speech_vote_payload(public)
        return super().make_revision(kind,document=doc,payload=public,**kwargs)

    def test_private_bundle_keeps_archive_while_v3_exports_only_selection(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        bundle=build_review_bundle(revision)
        self.assertEqual(bundle['format'],DIFFUSION_FORMAT)
        self.assertIn(PRIVATE_MARKER,json.dumps(bundle))
        with self.final_authority_settings(reviewer),patch.dict(os.environ,{
                'DV_SITE_VERIFICATION_KEY_ID':'synth-diffusion-v3',
                'DV_SITE_VERIFICATION_KEY':'PUBLIC-SYNTH-DIFFUSION-VECTOR-NOT-A-SECRET'}):
            with self.assertRaisesRegex(ValidationError,'site_export_refused'):
                build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            self.approve(dossier,revision,reviewer)
            packet=build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            self.assertEqual(packet['schema'],'dv.verification-site/3')
            self.assertNotIn(PRIVATE_MARKER,json.dumps(packet))
            self.assertNotIn('qualification_material',json.dumps(packet))
            self.assertNotIn('documentary_snapshot',packet['payload'])
            self.assertNotIn('fingerprint',packet['verification'])
            receipt=VerificationReceipt.objects.get(job__revision=revision)
            self.assertEqual(packet['verification']['private_fingerprint'],receipt.fingerprint)
            self.assertIn(PRIVATE_MARKER,json.dumps(receipt.job.input_snapshot))
            self.assertEqual(packet['verification']['diffusion_sha256'],sha256_digest(packet['payload']['diffusion_snapshot']))
            self.assertEqual(build_site_suspension(revision,user=verifier,environment='test',sequence=2,reason='SYNTH source withdrawn')['schema'],'dv.verification-site/3')
            with self.assertRaisesRegex(ValidationError,'diffusion_requires_verification_site_v3'):
                authorize_and_queue(revision,'test')
            stale=deepcopy(revision);stale.public_payload.pop('public_contract_version')
            with self.assertRaisesRegex(ValidationError,'diffusion_requires_verification_site_v3'):
                authorize_and_queue(stale,'test')
            self.assertFalse(PublicationSnapshot.objects.exists());self.assertFalse(OutboxEvent.objects.exists())

    def test_selected_values_and_private_source_changes_invalidate_current_receipt(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        original=deepcopy(revision.public_payload)
        for name in ('attribution','public_note','exact_quote','source_version'):
            public=deepcopy(original)
            if name=='public_note':public['diffusion_selection'][name]+=' Modification SYNTH.'
            elif name=='source_version':public['evidence'][0][name]['sha256']='a'*64
            else:public['diffusion_selection']['passages'][0][name]+=' Modification SYNTH.'
            revision.public_payload=public;revision.save(update_fields=["public_payload"])
            self.assertFalse(v.gate(revision)[0],name)
        revision.public_payload=original;revision.save(update_fields=["public_payload"])
        self.assertTrue(v.gate(revision)[0])

    def test_malformed_or_unbound_selection_refused(self):
        _,revision,_=self.make_revision();original=revision.public_payload
        variants=[]
        for value in (-1,True,100):
            changed=deepcopy(original);changed['diffusion_selection']['passages'][0]['evidence_index']=value;variants.append(changed)
        changed=deepcopy(original);changed['diffusion_selection']['passages'][0]['exact_quote']='Invented SYNTH';variants.append(changed)
        changed=deepcopy(original);changed['diffusion_selection']['passages'][0]['public_url']='https://example.org';variants.append(changed)
        changed=deepcopy(original);changed['declaration_source_url']='https://example.org/unbound';variants.append(changed)
        changed=deepcopy(original);changed['diffusion_selection']['passages'][0]['exact_quote']='x'*1201;variants.append(changed)
        for public in variants:
            with self.subTest(public=public['diffusion_selection']),self.assertRaises(ValidationError):validate_diffusion_selection(public)

    def test_projection_is_independent_copy_and_preview_does_not_approve(self):
        _,revision,_=self.make_revision()
        from atelier.services.diffusion import preview
        view=preview(revision);self.assertTrue(view['available'])
        selected=selected_public_payload(revision.public_payload)
        selected['evidence'][0]['source_version']['sha256']='d'*64
        self.assertNotEqual(selected['evidence'][0]['source_version']['sha256'],revision.public_payload['evidence'][0]['source_version']['sha256'])
        self.assertFalse(HumanReview.objects.exists());self.assertFalse(VerificationReceipt.objects.exists())

    def test_real_collective_binding_rebuilds_derived_fields_before_selection_check(self):
        from atelier.tests import test_catalogue_collective_scope as collective
        from atelier.services.catalogue import prepare_public_snapshot,binding_check,SPEECH_VOTE_DERIVED_KEYS
        helper=collective.CollectiveScopeTests();helper.setUp();revision=helper.make_bound()
        public=deepcopy(revision.public_payload);public['public_contract_version']=PUBLIC_CONTRACT
        public['diffusion_selection']={'contract':'dv.diffusion-selection/1','passages':[{'evidence_index':0,
            'exact_quote':public['declaration_quote'],'attribution':'SYNTH crédit collectif.'}],'public_note':'SYNTH limité.'}
        revision.public_payload=public
        self.assertTrue(binding_check(revision)[0],binding_check(revision))
        clean={key:value for key,value in public.items() if key not in SPEECH_VOTE_DERIVED_KEYS}
        rebuilt,refs=prepare_public_snapshot('speech_vote',clean,revision.source_snapshot,comparison=revision.comparison)
        self.assertEqual(rebuilt,public);self.assertEqual(refs,revision.source_snapshot)
        self.assertEqual(rebuilt['public_contract_version'],PUBLIC_CONTRACT)

    def test_preview_pending_is_not_used_for_failed_or_negative_examination(self):
        from atelier.services.diffusion import preview
        _,revision,_=self.make_revision();revision.verification_required=True;revision.save()
        job=v.enqueue(revision)
        self.assertTrue(preview(revision)['verification_pending'])
        for state in ('error','blocked','stale'):
            job.state=state;job.save(update_fields=['state'])
            result=preview(revision)
            self.assertFalse(result['verification_pending']);self.assertTrue(result['verification_blocked'])
