import base64
from copy import deepcopy
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.services.site_bridge import site_record_digest, verify_site_review
from atelier.tests.support import AtelierTestCase
import review_followup as q


class SiteReviewTests(SimpleTestCase):
    def test_shared_stored_vector_and_reference_site_schema(self):
        import json
        from pathlib import Path
        from django.conf import settings
        from jsonschema import Draft202012Validator, FormatChecker
        root = Path(settings.BASE_DIR)/"contracts"
        vector = json.loads((root/"site-ed25519-vector.synthetic.json").read_text(encoding="utf-8"))
        self.assertFalse(vector["real_human_review"])
        verify_site_review(vector["record"], trusted_public_keys=vector["public_keys"], environment="test", allow_test_fixtures=True)
        schema = json.loads((root/"site-bridge-draft1-reference.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate({"bridge_schema":"dv-site-bridge/0.1.0-draft.1",
            "batch_id":"SYNTHETIC:1", "idempotency_key":"SYNTHETIC:1", "generated_at":"2026-09-13T00:00:00Z", "records":[vector["record"]]})

    def fixture(self):
        # Clé de TEST aléatoire en mémoire ; jamais une identité ou clé du responsable éditorial.
        key = Ed25519PrivateKey.generate()
        record = {"record_type":"comparison", "record_id":"SYN:ED25519", "revision":1,
            "sequence":1, "operation":"upsert", "publication_circuit":"analysis", "is_test":True,
            "controls":{"citations":True}, "data":{"title":"Fictif é / \u2028 test"}}
        digest = site_record_digest(record)
        record["review"] = {"algorithm":"ed25519", "key_id":"test-ephemeral", "snapshot_sha256":digest,
            "signature":base64.b64encode(key.sign(digest.encode("ascii"))).decode("ascii")}
        return record, {"test-ephemeral":base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")}

    def test_external_proof_binds_revision_data_but_not_transport_sequence(self):
        record, keys = self.fixture()
        verify = lambda r: verify_site_review(r, trusted_public_keys=keys, environment="test", allow_test_fixtures=True)
        self.assertEqual(verify(record), record["review"]["snapshot_sha256"])
        changed = deepcopy(record)
        changed["sequence"] = 2
        self.assertEqual(verify(changed), verify(record))
        for field, value in (("revision", 2), ("record_id", "SYN:OTHER")):
            changed = deepcopy(record)
            changed[field] = value
            with self.assertRaisesMessage(ValidationError, "site_review_snapshot_mismatch"):
                verify(changed)

    def test_test_proof_rejected_in_production_or_without_opt_in(self):
        record, keys = self.fixture()
        for environment, fixtures in (("production", True), ("staging", False)):
            with self.assertRaisesMessage(ValidationError, "site_test_review_rejected"):
                verify_site_review(record, trusted_public_keys=keys, environment=environment, allow_test_fixtures=fixtures)

    def test_hmac_boolean_unknown_key_and_other_payload_not_human_proof(self):
        record, keys = self.fixture()
        for proof in (True, {**record["review"], "algorithm":"HMAC-SHA256"},
                      {**record["review"], "key_id":"unknown"},
                      {**record["review"], "signature":base64.b64encode(b"0"*64).decode()}):
            bad = {**record, "review":proof}
            with self.assertRaises(ValidationError):
                verify_site_review(bad, trusted_public_keys=keys, environment="test", allow_test_fixtures=True)


@override_settings(DV_ENV="test", DV_EVENT_KEY_ID="test-v1", DV_REVIEW_ATTESTATION_KEY="synthetic-handoff-only",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteHandoffTests(AtelierTestCase):
    make_catalogue_revision = q.FollowupTests.make_catalogue_revision

    def test_projection_exact_site_schema_proof_and_no_send_authorization(self):
        from atelier.services.site_bridge import prepare_comparison_handoff
        from atelier.services.publication import authorize_and_queue
        from django.conf import settings
        from jsonschema import Draft202012Validator, FormatChecker
        import json
        from pathlib import Path
        _, factual, document, _ = self.make_catalogue_revision()
        public = self.payload("speech_vote")
        public["catalogue"] = factual.public_payload["catalogue"]
        public["attribution"] = {"person_id":"SYN:ALPHA", "organization_id":None,
            "speaker_label":"Personne fictive Alpha", "capacity":"rôle fictif"}
        dossier, revision, _ = self.make_revision("speech_vote", document=document, payload=public)
        reviewer = self.make_user()
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
            handoff = prepare_comparison_handoff(event)
            self.assertFalse(handoff["external_ed25519_verified"])
            record = handoff["site_record_candidate"]
            key = Ed25519PrivateKey.generate()
            proof = {"algorithm":"ed25519", "key_id":"test-handoff", "snapshot_sha256":site_record_digest(record),
                "signature":base64.b64encode(key.sign(site_record_digest(record).encode("ascii"))).decode()}
            verified = prepare_comparison_handoff(event, detached_review=proof,
                trusted_public_keys={"test-handoff":base64.b64encode(key.public_key().public_bytes_raw()).decode()})
        schema = json.loads((Path(settings.BASE_DIR)/"contracts/site-bridge-draft1-reference.schema.json").read_text(encoding="utf-8"))
        batch = {"bridge_schema":handoff["target_bridge"], "batch_id":"SYN:BATCH", "idempotency_key":"SYN:BATCH",
            "generated_at":"2026-09-13T00:00:00Z", "records":[verified["site_record_candidate"]]}
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(batch)
        self.assertTrue(verified["external_ed25519_verified"])
        self.assertFalse(verified["send_authorized"])
        self.assertFalse(verified["publication_authorized"])
        self.assertTrue(verified["blocking_gaps"])
