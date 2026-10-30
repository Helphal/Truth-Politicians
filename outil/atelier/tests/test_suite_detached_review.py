import base64,json
from copy import deepcopy
from unittest.mock import patch
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings,Client
from django.utils import timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.models import DetachedReviewEvidence,HumanReview
from atelier.tests.support import AtelierTestCase
from atelier.services.full_review import build_review_bundle,approval_bytes
from atelier.services.canonical import sha256_digest
from atelier.services.detached_review import accept_detached_review,verify_current_receipt
from atelier.services.publication import evaluate_revision,authorize_and_queue,record_human_review

class DetachedReviewTests(AtelierTestCase):
    def fixture(self):
        dossier,revision,_=self.make_revision('speech_vote')
        user=self.make_user();bundle=build_review_bundle(revision)
        key=Ed25519PrivateKey.generate();keys={'test-offline':base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        statement={'algorithm':'ed25519','key_id':'test-offline','bundle_sha256':sha256_digest(bundle),'record_id':bundle['record_id'],'record_revision':bundle['record_revision'],'decision':'approve','reviewed_at':timezone.now().isoformat()}
        proof={'statement':statement,'signature':base64.b64encode(key.sign(approval_bytes(bundle,statement))).decode()}
        return dossier,revision,user,bundle,proof,keys
    def accept(self,d,r,u,b,p):
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=str(u.dv_profile.internal_uid)):
            return accept_detached_review(dossier_id=d.pk,expected_revision=r.revision,bundle=b,proof=p,user=u,environment='test',destination='dv-site:test')
    def test_return_audit_eligibility_replay_revoke(self):
        d,r,u,b,p,k=self.fixture()
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=k):
            review=self.accept(d,r,u,b,p)
            holder=str(u.dv_profile.internal_uid)
            with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
                self.assertTrue(verify_current_receipt(review))
                self.assertTrue(evaluate_revision(r,'test').allowed)
                self.assertEqual(self.accept(d,r,u,b,p).pk,review.pk)
                event=authorize_and_queue(r,'test')
                self.assertEqual(event.environment,'test')
                record_human_review(
                    dossier_id=d.pk,
                    expected_revision=r.revision,
                    expected_digest=r.snapshot_digest,
                    user=u,
                    decision='request_changes',
                    environment='test',
                )
                self.assertFalse(evaluate_revision(r,'test').allowed)
            with self.assertRaisesMessage(ValidationError,'detached_return_revoked'):self.accept(d,r,u,b,p)
            self.assertEqual(DetachedReviewEvidence.objects.count(),1)
    def test_wrong_key_modified_reserve_or_target_no_review(self):
        d,r,u,b,p,k=self.fixture()
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=k):
            for field in ('main_caveat','counterevidence'):
                altered=deepcopy(b);altered['public'][field]='changed'
                with self.subTest(field=field),self.assertRaises(ValidationError):self.accept(d,r,u,altered,p)
            wrong=deepcopy(b);wrong['destination']='other'
            with self.assertRaises(ValidationError):self.accept(d,r,u,wrong,p)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS={}):
            with self.assertRaises(ValidationError):self.accept(d,r,u,b,p)
        self.assertEqual(HumanReview.objects.count(),0)
    def test_real_scope_or_environment_refused(self):
        d,r,u,b,p,k=self.fixture()
        with self.assertRaises(PermissionDenied):
            accept_detached_review(dossier_id=d.pk,expected_revision=1,bundle=b,proof=p,user=u,environment='production',destination='dv-site:production')
        d.data_scope='real';d.save(update_fields=['data_scope'])
        with self.assertRaises(PermissionDenied):self.accept(d,r,u,b,p)
    def test_interrupted_return_rolls_back_human_review(self):
        d,r,u,b,p,k=self.fixture()
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=k),patch('atelier.services.detached_review.DetachedReviewEvidence.objects.create',side_effect=ValidationError('interrupted')):
            with self.assertRaises(ValidationError):self.accept(d,r,u,b,p)
        self.assertEqual(HumanReview.objects.count(),0)
    def test_http_download_return_and_csrf(self):
        d,r,u,b,p,k=self.fixture();url=f'/dossiers/{d.pk}/signature-test/'
        holder=str(u.dv_profile.internal_uid)
        self.assertEqual(self.client.get(url).status_code,302);self.client.force_login(u)
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            self.assertEqual(self.client.get(url+'?download=bundle').json(),b)
            with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=k):
                upload=SimpleUploadedFile('signed.json',json.dumps({'bundle':b,'proof':p}).encode(),content_type='application/json')
                self.assertEqual(self.client.post(url,{'signed_review':upload}).status_code,302)
            client=Client(enforce_csrf_checks=True);client.force_login(u)
            self.assertEqual(client.post(url,{}).status_code,403)
    def test_new_revision_invalidates_saved_receipt(self):
        d,r,u,b,p,k=self.fixture()
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=k):
            self.accept(d,r,u,b,p)
            d.current_revision=2;d.save(update_fields=['current_revision'])
            self.assertFalse(evaluate_revision(r,'test').allowed)
            with self.assertRaises(ValidationError):self.accept(d,r,u,b,p)
