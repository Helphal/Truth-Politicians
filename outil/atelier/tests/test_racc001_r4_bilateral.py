"""R4 — revue détachée TEST, transport, confirmation de visibilité (DV-RACC-001).

Les tests qui appellent `_site_confirmation` couvrent le consommateur Outil uniquement :
R4 bilatéral reste PARTIEL tant que la confirmation n’est pas émise par WordPress (cf. R5 live).
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone

from atelier.models import Declaration
from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.services.detached_review import accept_detached_review
from atelier.services.editorial_feed import DOMAIN, FORMAT, accept_confirmation, verify_confirmation
from atelier.services.full_review import approval_bytes, build_review_bundle
from atelier.services.publication import authorize_and_queue, evaluate_revision, packet_for_event, queue_suspension
from atelier.services.site_bridge import prepare_comparison_handoff
from atelier.tests.racc001_php import RACC001_PRESS_PUBLIC_URL
from atelier.tests.test_racc001_corpus import Racc001CorpusTests

@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Racc001R4BilateralTests(Racc001CorpusTests):
    def _detached_approve(self, dossier, revision, user, keys):
        bundle = build_review_bundle(revision, environment="test", destination="dv-site:test")
        key = Ed25519PrivateKey.generate()
        review_keys = {**keys, "test-racc4": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        statement = {
            "algorithm": "ed25519",
            "key_id": "test-racc4",
            "bundle_sha256": sha256_digest(bundle),
            "record_id": dossier.stable_id,
            "record_revision": revision.revision,
            "decision": "approve",
            "reviewed_at": timezone.now().isoformat(),
        }
        proof = {
            "statement": statement,
            "signature": base64.b64encode(key.sign(approval_bytes(bundle, statement))).decode(),
        }
        holder = str(user.dv_profile.internal_uid)
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=review_keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
        ):
            accept_detached_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                bundle=bundle,
                proof=proof,
                user=user,
                environment="test",
                destination="dv-site:test",
            )
        return review_keys

    def _site_confirmation(self, event):
        key = Ed25519PrivateKey.generate()
        site_keys = {"test-site-racc4": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        statement = {
            "format": FORMAT,
            "event_id": str(event.event_id),
            "event_digest": event.payload_digest,
            "record_id": event.dossier.stable_id,
            "revision": event.content_revision,
            "environment": "test",
            "destination": "dv-site:test",
            "status": "published",
            "published_at": timezone.now().isoformat(),
            "public_url": RACC001_PRESS_PUBLIC_URL,
            "change_kind": "initial",
            "key_id": "test-site-racc4",
        }
        signature = base64.b64encode(key.sign(DOMAIN + canonical_bytes(statement))).decode()
        return statement, signature, site_keys

    def test_review_before_transport_and_exact_envelope(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = {}
        self.assertFalse(evaluate_revision(revision, "test").allowed)
        with self.assertRaises(ValidationError):
            authorize_and_queue(revision, "test")
        keys = self._detached_approve(dossier, revision, user, keys)
        holder = str(user.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            self.assertTrue(evaluate_revision(revision, "test").allowed)
            event = authorize_and_queue(revision, "test")
            packet = packet_for_event(event)
        self.assertIn("signature", packet)
        self.assertNotIn("receipt_schema_version", packet)
        bundle = build_review_bundle(revision)
        tampered = deepcopy(bundle)
        tampered["public"]["title"] = tampered["public"]["title"] + " altéré"
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            with self.assertRaises(ValidationError):
                accept_detached_review(
                    dossier_id=dossier.pk,
                    expected_revision=revision.revision,
                    bundle=tampered,
                    proof={"statement": {}, "signature": "a"},
                    user=user,
                    environment="test",
                    destination="dv-site:test",
                )

    def test_visibility_confirmation_not_transport_receipt(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event = authorize_and_queue(revision, "test")
            packet = packet_for_event(event)
            statement, signature, site_keys = self._site_confirmation(event)
        with override_settings(
            DV_SITE_PUBLICATION_PUBLIC_KEYS=site_keys,
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
        ):
            verify_confirmation(event, statement, signature)
            with self.assertRaises(ValidationError):
                accept_confirmation(event, {"status": "applied"}, signature)
            wrong_dest = deepcopy(statement)
            wrong_dest["destination"] = "dv-site:production"
            with self.assertRaises(ValidationError):
                accept_confirmation(event, wrong_dest, signature)
            accept_confirmation(event, statement, signature)
        self.assertNotEqual(packet["signature"], signature)
        self.assertEqual(packet["payload_digest"], event.payload_digest)

    def test_handoff_after_review_and_refuse_wrong_destination(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.COLLECTIVE
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        with self.assertRaises(ValidationError):
            authorize_and_queue(revision, "test")
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event = authorize_and_queue(revision, "test")
            handoff = prepare_comparison_handoff(event)
        self.assertFalse(handoff["send_authorized"])
        bundle = build_review_bundle(revision)
        with self.assertRaises(ValidationError):
            verify_detached_review = __import__(
                "atelier.services.full_review", fromlist=["verify_detached_review"]
            ).verify_detached_review
            verify_detached_review(
                bundle,
                {"statement": {}, "signature": "a"},
                trusted_public_keys=keys,
                environment="test",
                destination="dv-site:production",
                allow_test_fixtures=True,
            )

    def test_suspension_and_stale_revision_refuse_visibility(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event = authorize_and_queue(revision, "test")
            statement, signature, site_keys = self._site_confirmation(event)
        with override_settings(
            DV_SITE_PUBLICATION_PUBLIC_KEYS=site_keys,
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
        ):
            accept_confirmation(event, statement, signature)
            queue_suspension(dossier, "Retrait documentaire de test.", environment="test")
            with self.assertRaises(ValidationError):
                accept_confirmation(event, statement, signature)
        stale = deepcopy(statement)
        stale["revision"] = revision.revision + 1
        with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=site_keys):
            with self.assertRaises(ValidationError):
                verify_confirmation(event, stale, signature)
