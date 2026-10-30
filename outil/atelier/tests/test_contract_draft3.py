import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from django.conf import settings
from django.utils import timezone
from jsonschema import Draft202012Validator, FormatChecker
from atelier.models import OutboxEvent
from atelier.services.canonical import sha256_digest
from atelier.services.contracts import publication_schema, validate_contract_envelope, validate_semantic_envelope
from atelier.services.exporter import export_outbox
from atelier.services.publication import packet_for_event
from atelier.services.receiver import apply_packet
from django.test import override_settings
from atelier.tests import test_counter_review as boundary
from atelier.tests.support import AtelierTestCase


# Shared fixture methods only; imported parent test cases must not be duplicated.
@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled", DV_EVENT_KEY_ID="test-v1",
    DV_RECEIPT_KEY_ID="test-v1", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DV_REVIEW_ATTESTATION_KEY="synthetic-counter-review-not-a-secret")
class Draft3Tests(AtelierTestCase):
    make_packet = boundary.CounterReviewBoundaryTests.make_packet
    configured_send = boundary.CounterReviewBoundaryTests.configured_send
    resign = staticmethod(boundary.CounterReviewBoundaryTests.resign)

    def real_packet(self):
        from atelier.models import EditorialDossier
        from atelier.services.catalogue import official_fields
        from atelier.services.revisions import create_revision, record_controls, deterministic_control_results
        from atelier.services.publication import authorize_and_queue
        document = self.make_document()
        document.source.qualified_at = timezone.now()
        document.source.terms_url = "https://www.assemblee-nationale.fr/licence"
        document.source.save()
        scrutiny = self.make_scrutiny(document)
        dossier = EditorialDossier.objects.create(stable_id="REAL:CONTRACT", content_kind="official_vote", established_kind="official_vote", data_scope="real")
        revision = create_revision(dossier=dossier, public_payload={**official_fields(scrutiny), "coverage": self.coverage(), "themes": []},
            source_snapshot=self.snapshot(document), title="Factuel", short_answer="", main_caveat="", reasoning="")
        record_controls(revision, deterministic_control_results(revision))
        return packet_for_event(authorize_and_queue(revision, "test"))

    def test_catalogue_relation_and_source_version_fail_closed(self):
        packet = self.real_packet()
        for mutate in (
            lambda public: public["catalogue"]["text_versions"][0].update(dossier_id="UNKNOWN:DOSSIER"),
            lambda public: public["catalogue"]["text_versions"][0]["source_version"].update(sha256="0" * 64),
            lambda public: public.update(official_title="Autre titre tout aussi plausible"),
        ):
            changed = deepcopy(packet)
            mutate(changed["payload"]["public"])
            changed["payload"]["authorization"]["snapshot_digest"] = sha256_digest(changed["payload"]["public"])
            self.resign(changed)
            self.assertEqual(apply_packet(changed), "public_control_failed")
    def test_signed_forged_counts_fail_even_with_all_digests_recomputed(self):
        *_, packet = self.make_packet()
        packet["payload"]["public"]["counts"]["voters"]["value"] = 0
        packet["payload"]["authorization"]["snapshot_digest"] = sha256_digest(packet["payload"]["public"])
        self.resign(packet)
        self.assertEqual(apply_packet(packet), "public_control_failed")

    def test_delivery_is_not_revoked_merely_because_transport_grant_expires(self):
        *_, event, packet = self.make_packet()
        self.configured_send(packet, lambda p, **kw: __import__("atelier.services.receipts", fromlist=["receipt_for_packet"]).receipt_for_packet(p, "applied"))
        OutboxEvent.objects.filter(pk=event.pk).update(issued_at=timezone.now() - timedelta(days=1))
        with TemporaryDirectory() as tmp:
            self.assertEqual(export_outbox(Path(tmp) / "batch", environment="test")["count"], 0)
        event.refresh_from_db()
        self.assertEqual(event.state, "delivered")
        self.assertFalse(OutboxEvent.objects.filter(operation="suspend").exists())

    def test_expired_pending_event_gets_new_identity_after_fresh_authority_check(self):
        *_, event, packet = self.make_packet()
        OutboxEvent.objects.filter(pk=event.pk).update(issued_at=timezone.now() - timedelta(days=1))
        with TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / "batch", environment="test")
        self.assertEqual(manifest["count"], 1)
        self.assertNotEqual(manifest["items"][0]["event_id"], packet["event_id"])
        event.refresh_from_db()
        self.assertEqual(event.last_error_code, "superseded_expired_event")
        self.assertFalse(OutboxEvent.objects.filter(operation="suspend").exists())
        from atelier.services.publication import authorize_and_queue
        self.assertEqual(str(authorize_and_queue(event.snapshot.revision, "test").event_id), manifest["items"][0]["event_id"])

    def test_expired_and_revoked_event_cannot_be_reissued(self):
        *_, review, event, packet = self.make_packet(speech=True)
        review.revoked = True
        review.save()
        OutboxEvent.objects.filter(pk=event.pk).update(issued_at=timezone.now() - timedelta(days=1))
        with TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / "batch", environment="test")
        self.assertEqual([item["operation"] for item in manifest["items"]], ["suspend"])

    def test_catalogue_definitions_are_reachable_from_root(self):
        schema = publication_schema()
        visited = set()
        def walk(node):
            if isinstance(node, dict):
                ref = node.get("$ref", "")
                if ref.startswith("#/$defs/") and ref not in visited:
                    visited.add(ref)
                    walk(schema["$defs"][ref.split("/")[-1]])
                for k, v in node.items():
                    if k != "$defs":
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(schema)
        for name in ("person", "organization", "affiliation", "mandate", "textVersion", "voteObservation", "comparisonAct", "evidenceItem"):
            self.assertIn("#/$defs/" + name, visited)

    def test_new_contract_examples_and_receipt_schema(self):
        for path in (Path(settings.BASE_DIR) / "contracts/examples-draft3").glob("*.synthetic.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            validate_contract_envelope(body)
            validate_semantic_envelope(body)
            self.assertEqual(body["authorization"]["snapshot_digest"], sha256_digest(body["public"]))
        receipt_schema = json.loads((Path(settings.BASE_DIR) / "contracts/dv-receipt-0.1.0-draft.1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(receipt_schema)
        *_, packet = self.make_packet()
        from atelier.services.receipts import receipt_for_packet
        Draft202012Validator(receipt_schema, format_checker=FormatChecker()).validate(receipt_for_packet(packet, "applied"))

    def test_shared_canonical_and_signature_vectors(self):
        import hashlib
        import hmac
        from atelier.services.canonical import canonical_bytes
        from atelier.services.contracts import validate_event_packet_schema
        vector = json.loads((Path(settings.BASE_DIR) / "contracts/canonical-draft3-vectors.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical_bytes(vector["value"]).hex(), vector["canonical_utf8_hex"])
        self.assertEqual(sha256_digest(vector["value"]), vector["sha256"])
        validate_event_packet_schema(vector["event"])
        for packet in (vector["event"], vector["receipt"]):
            unsigned = {k: v for k, v in packet.items() if k not in {"signature", "signature_algorithm"}}
            self.assertEqual(hmac.new(vector["test_key_material"].encode(), canonical_bytes(unsigned), hashlib.sha256).hexdigest(), packet["signature"])
        self.assertEqual(sha256_digest(vector["event"]), vector["receipt"]["event_digest"])
