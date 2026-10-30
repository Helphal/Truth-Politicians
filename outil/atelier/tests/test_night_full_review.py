import base64
from copy import deepcopy
from django.test import SimpleTestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.services.canonical import sha256_digest
from atelier.services.full_review import FORMAT,approval_bytes,verify_detached_review

class FullReviewTests(SimpleTestCase):
    def fixture(self):
        public={'title':'FICTIF','evidence':[{'quote':'Preuve fictive'}],'counterevidence':[{'statement':'Réserve fictive'}],'catalogue':{'persons':[{'id':'TEST-PERSON','name':'Fictif'}]}}
        bundle={'format':FORMAT,'record_id':'TEST-RECORD','record_revision':2,'environment':'test','destination':'dv-site:test','content_kind':'speech_vote','data_scope':'synthetic','snapshot_digest':sha256_digest(public),'public':public,'source_versions':[{'sha256':'a'*64}],'method_version':'TEST'}
        statement={'algorithm':'ed25519','key_id':'test-ephemeral','bundle_sha256':sha256_digest(bundle),'record_id':'TEST-RECORD','record_revision':2,'decision':'approve','reviewed_at':timezone.now().isoformat()}
        key=Ed25519PrivateKey.generate()
        proof={'statement':statement,'signature':base64.b64encode(key.sign(approval_bytes(bundle,statement))).decode()}
        return bundle,proof,{'test-ephemeral':base64.b64encode(key.public_key().public_bytes_raw()).decode()}
    def verify(self,b,p,k,**kwargs):return verify_detached_review(b,p,trusted_public_keys=k,environment='test',destination='dv-site:test',allow_test_fixtures=True,**kwargs)
    def test_valid_full_binding_does_not_authorize_send(self):
        b,p,k=self.fixture();result=self.verify(b,p,k)
        self.assertTrue(result['verified_detached_signature']);self.assertFalse(result['send_authorized'])
    def test_every_substantive_family_bound(self):
        for field in ('evidence','counterevidence','catalogue'):
            b,p,k=self.fixture();b['public'][field]=[];b['snapshot_digest']=sha256_digest(b['public'])
            with self.subTest(field=field),self.assertRaisesMessage(ValidationError,'full_review_binding_mismatch'):self.verify(b,p,k)
    def test_sources_and_revision_bound(self):
        for field in ('source_versions','record_revision'):
            b,p,k=self.fixture();b[field]=[] if field=='source_versions' else 3
            with self.subTest(field=field),self.assertRaisesMessage(ValidationError,'full_review_binding_mismatch'):self.verify(b,p,k)
    def test_signature_not_transport_hmac_or_importer_boolean(self):
        b,p,k=self.fixture();p['signature']=base64.b64encode(b'0'*64).decode()
        with self.assertRaisesMessage(ValidationError,'full_review_signature_invalid'):self.verify(b,p,k)
        with self.assertRaisesMessage(ValidationError,'full_review_proof_invalid'):self.verify(b,{'approved':True},k)
    def test_fixture_never_production(self):
        b,p,k=self.fixture()
        with self.assertRaisesMessage(ValidationError,'full_review_target_mismatch'):verify_detached_review(b,p,trusted_public_keys=k,environment='production',destination='dv-site:production',allow_test_fixtures=True)
