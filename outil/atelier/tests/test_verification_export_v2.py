"""SYNTH: export the exact verified server projection, with distinct TEST review."""
from copy import deepcopy
import os
from unittest.mock import patch
from django.core.exceptions import ValidationError
from atelier.models import VerificationReceipt
from atelier.services.canonical import sha256_digest
from atelier.services.verification_export import build_site_packet,build_site_suspension,_documentary_snapshot,SITE_SCHEMA
from atelier.tests.test_verification_counterreview_v2 import ExportCounterReviewTests


class DocumentaryExportTests(ExportCounterReviewTests):
    def test_complete_server_snapshot_is_exported_after_review_only(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        with self.final_authority_settings(reviewer),patch.dict(os.environ,{
            'DV_SITE_VERIFICATION_KEY_ID':'synth-doc-v2',
            'DV_SITE_VERIFICATION_KEY':'PUBLIC-SYNTH-DOCUMENTARY-VECTOR-NOT-A-SECRET'}):
            with self.assertRaisesRegex(ValidationError,'site_export_refused'):
                build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            self.approve(dossier,revision,reviewer)
            packet=build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping)
            receipt=VerificationReceipt.objects.select_related('job').get(job__revision=revision)
            self.assertEqual(packet['schema'],SITE_SCHEMA)
            self.assertEqual(packet['payload']['documentary_snapshot']['content'],receipt.job.input_snapshot['content'])
            self.assertEqual(packet['verification']['documentary_sha256'],sha256_digest(packet['payload']['documentary_snapshot']))
            self.assertEqual(packet['payload']['evidence'],revision.reasoning)
            self.assertEqual(build_site_packet(revision,user=verifier,environment='test',sequence=1,mapping=mapping),packet)
            withdrawal=build_site_suspension(revision,user=verifier,environment='test',sequence=2,reason='SYNTH retrait')
            self.assertEqual(withdrawal['schema'],SITE_SCHEMA)
            self.assertEqual(withdrawal['operation'],'suspend')

    def test_altered_or_omitted_verified_projection_is_not_exportable(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        receipt=VerificationReceipt.objects.select_related('job').get(job__revision=revision)
        original=deepcopy(receipt.job.input_snapshot)
        for replacement in ({}, {**original['content']['public_payload'],'catalogue':{'scope_person_ids':['SYNTH-FAKE']}}):
            receipt.job.input_snapshot=deepcopy(original)
            receipt.job.input_snapshot['content']['public_payload']=replacement
            with self.assertRaisesRegex(ValidationError,'not_verified'):_documentary_snapshot(revision,receipt)

    def test_documentary_builder_copies_and_preserves_unknown_future_scope_keys(self):
        dossier,revision,verifier,reviewer,mapping=self.prepare()
        receipt=VerificationReceipt.objects.select_related('job').get(job__revision=revision)
        # Serialization unit only: a future server-verified input is supplied to
        # the helper, without creating a receipt or bypassing an export gate.
        checked=deepcopy(receipt.job.input_snapshot)
        checked['content']['public_payload']['catalogue']={'scope_person_ids':['SYNTH:P1','SYNTH:P2'],
            'future_scope_descriptor':{'organization_id':'SYNTH:GROUP','as_of':'2023-02-06'}}
        receipt.job.input_snapshot=checked
        receipt.job.fingerprint=receipt.fingerprint=sha256_digest(checked)
        with patch('atelier.services.verification_export.snapshot',return_value=deepcopy(checked)):
            document=_documentary_snapshot(revision,receipt)
        self.assertEqual(document['content']['public_payload'],checked['content']['public_payload'])
        document['content']['public_payload']['SYNTH-local-mutation']=True
        self.assertNotIn('SYNTH-local-mutation',receipt.job.input_snapshot['content']['public_payload'])
