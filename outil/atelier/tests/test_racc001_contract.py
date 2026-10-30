"""DV-RACC-001 R2 — contrat racc001-draft.1 et empreintes C01–C04."""
import copy
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase

from atelier.models import EditorialDossier
from atelier.services.contracts import publication_schema_for, validate_contract_envelope
from atelier.services.publication import authorize_and_queue
from atelier.services.racc001_contract import enrich_publication_envelope, qualification_fingerprints
from atelier.tests.support import AtelierTestCase


class Racc001ContractSchemaTests(TestCase):
    def test_schema_meta_and_fingerprints_def(self):
        schema = publication_schema_for("dv.content/0.4.0-racc001-draft.1")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "dv.content/0.4.0-racc001-draft.1")
        self.assertIn("qualificationFingerprints", schema["$defs"])


class Racc001ContractExportTests(AtelierTestCase):
    @staticmethod
    def _minimal_racc_public(public: dict) -> dict:
        qualified = {
            "state": "known",
            "precision": "day",
            "value": public["declaration_date"],
            "interval_start": None,
            "interval_end": None,
        }
        actors = {
            "speaker": {"person_id": "SYN:PERSON", "label": "TEST", "capacity": "x"},
            "represented_entity": {"organization_id": None},
            "compared_actor": {"person_id": "SYN:PERSON", "organization_id": None, "label": "TEST"},
            "scope": "personal",
        }
        return {
            "actors": actors,
            "declaration_date_precision": "day",
            "compared_actor_scope": "personal",
            "qualified_declaration": qualified,
            "qualification_material": {
                "declaration_id": "SYN:DECL",
                "qualified_declaration": qualified,
                "actors": actors,
                "declaration_measure_links": [],
                "measure_scrutiny_links": [],
                "declaration_source": {
                    "public_url": public["declaration_source_url"],
                    "locator": "fixture",
                    "document_version": "v-test",
                    "sha256": "c" * 64,
                },
            },
        }

    def test_speech_vote_export_carries_actors_and_fingerprints(self):
        reviewer = self.make_user()
        public = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        public["attribution"] = {
            "person_id": "SYN:PERSON",
            "organization_id": "SYN:ORG",
            "speaker_label": "Porte-parole TEST",
            "capacity": "rapporteur fictif",
        }
        qualified = {
            "state": "known",
            "precision": "day",
            "value": public["declaration_date"],
            "interval_start": None,
            "interval_end": None,
        }
        public["actors"] = {
            "speaker": {
                "person_id": "SYN:PERSON",
                "label": "Porte-parole TEST",
                "capacity": "rapporteur fictif",
            },
            "represented_entity": {"organization_id": "SYN:ORG"},
            "compared_actor": {
                "person_id": None,
                "organization_id": "SYN:ORG",
                "label": "Porte-parole TEST",
            },
            "scope": "collective",
        }
        public["compared_actor_scope"] = "collective"
        public["declaration_date_precision"] = "day"
        public["qualified_declaration"] = qualified
        public["qualification_material"] = {
            "declaration_id": "SYN:DECL",
            "qualified_declaration": qualified,
            "actors": public["actors"],
            "declaration_measure_links": [],
            "measure_scrutiny_links": [],
            "declaration_source": {
                "public_url": public["declaration_source_url"],
                "locator": "fixture",
                "document_version": "v-test",
                "sha256": "b" * 64,
            },
        }
        dossier, revision, _ = self.make_revision(
            EditorialDossier.Kind.SPEECH_VOTE, payload=public, environment="test"
        )
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
        body = event.payload
        self.assertEqual(body["schema_version"], settings.DV_PUBLICATION_SCHEMA)
        self.assertIn("actors", body["public"])
        self.assertIn("qualification_fingerprints", body["authorization"])
        validate_contract_envelope(body)

    def test_negative_unknown_schema_rejected(self):
        reviewer = self.make_user()
        public = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        public.update(self._minimal_racc_public(public))
        dossier, revision, _ = self.make_revision(
            EditorialDossier.Kind.SPEECH_VOTE, payload=public, environment="test"
        )
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
        bad = copy.deepcopy(event.payload)
        bad["schema_version"] = "dv.content/unknown"
        with self.assertRaises(ValidationError):
            validate_contract_envelope(bad)

    def test_event_suspend_schema_bilateral(self):
        from atelier.services.contracts import validate_event_packet_schema

        legacy = {
            "event_schema_version": "dv.event/0.2.0-draft.1",
            "environment": "test",
            "destination": "dv-site:test",
            "key_id": "k",
            "expires_at": "2099-01-01T00:00:00Z",
            "event_id": "00000000-0000-4000-8000-000000000011",
            "sequence": 1,
            "operation": "suspend",
            "record_id": "EDITORIAL:EVT",
            "record_revision": 1,
            "payload_digest": "a" * 64,
            "signature_algorithm": "HMAC-SHA256",
            "signature": "b" * 64,
            "payload": {
                "schema_version": "dv.content/0.4.0-draft.3",
                "record_id": "EDITORIAL:EVT",
                "record_revision": 1,
                "operation": "suspend",
                "public_reason": "legacy ok",
            },
        }
        validate_event_packet_schema(legacy)
        racc = dict(legacy)
        racc["event_schema_version"] = "dv.event/0.2.0-racc001-draft.1"
        racc["event_id"] = "00000000-0000-4000-8000-000000000012"
        racc["payload"] = {
            "schema_version": settings.DV_PUBLICATION_SCHEMA,
            "record_id": "EDITORIAL:EVT",
            "record_revision": 1,
            "operation": "suspend",
            "public_reason": "racc001 ok",
        }
        validate_event_packet_schema(racc)
        rejected = dict(legacy)
        rejected["payload"]["schema_version"] = settings.DV_PUBLICATION_SCHEMA
        with self.assertRaises(ValidationError):
            validate_event_packet_schema(rejected)

    def test_manifest_schema_sha(self):
        path = Path(settings.BASE_DIR) / "contracts/dv-content-0.4.0-racc001-draft.1.schema.json"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = json.loads(
            (Path(settings.BASE_DIR) / "contracts/racc001-schema-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["sha256"], digest)
