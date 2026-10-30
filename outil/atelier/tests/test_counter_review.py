"""Cas supplémentaires indépendants des assertions livrées dans la contre-revue."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from atelier.models import EditorialDossier, HumanReview, OutboxEvent, Source, ReceiverCursor
from atelier.services.catalogue import official_fields, binding_check
from atelier.services.canonical import sha256_digest
from atelier.services.exporter import export_outbox
from atelier.services.factual_checks import counts_check, calculation_check
from atelier.services.publication import authorize_and_queue, evaluate_revision, packet_for_event, record_human_review, sign_packet
from atelier.services.receipts import receipt_for_packet
from atelier.services.receiver import apply_packet
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.services.wordpress import send_packet, WordPressUnavailable
from atelier.tests.support import AtelierTestCase


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled", DV_EVENT_KEY_ID="test-v1",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DV_REVIEW_ATTESTATION_KEY="synthetic-counter-review-not-a-secret")
class CounterReviewBoundaryTests(AtelierTestCase):
    @staticmethod
    def resign(packet):
        packet["payload_digest"] = sha256_digest(packet["payload"])
        packet["signature"] = sign_packet({k: v for k, v in packet.items() if k not in {"signature_algorithm", "signature"}})
        return packet

    def make_packet(self, speech=False):
        dossier, revision, document = self.make_revision("speech_vote" if speech else "official_vote")
        reviewer = self.make_user() if speech else None
        if speech:
            review = self.approve(dossier, revision, reviewer)
            with self.final_authority_settings(reviewer):
                event = authorize_and_queue(revision, "test")
                packet = packet_for_event(event)
        else:
            review = None
            event = authorize_and_queue(revision, "test")
            packet = packet_for_event(event)
        return dossier, revision, document, review, event, packet

    def configured_send(self, packet, transport):
        with override_settings(DV_NETWORK_MODE="direct", DV_PUBLICATION_SEND_AUTHORIZED=True, DV_RECEIVER_STATE_RECONCILED=True), patch.dict(os.environ, {
            "DV_WORDPRESS_ENDPOINT": "https://example.org/test-only", "DV_WORDPRESS_USERNAME": "fixture", "DV_WORDPRESS_APP_PASSWORD": "fixture-only"}), patch("atelier.services.wordpress._post_packet", side_effect=transport) as post:
            result = send_packet(packet)
            return result, post.call_count

    def test_false_counts_and_unqualified_procedure_are_not_asserted_checked(self):
        payload = self.payload("official_vote")
        payload["counts"]["voters"]["value"] = 1
        self.assertFalse(counts_check(payload)[0])
        payload = self.payload("official_vote")
        payload["procedure"] = "procédure non qualifiée"
        self.assertEqual(counts_check(payload), (False, "count_procedure_not_qualified"))

    def test_unknown_counts_do_not_become_zero_or_abstention(self):
        payload = self.payload("official_vote")
        payload["counts"]["for"] = self.state_count(None, "unknown")
        self.assertTrue(counts_check(payload)[0])
        self.assertIsNone(payload["counts"]["for"]["value"])

    def test_calculation_boolean_universe_and_duplicate_are_rejected(self):
        payload = self.payload("vote_comparison")
        payload["calculation"]["numerator"] = True
        self.assertFalse(calculation_check(payload)[0])
        payload = self.payload("vote_comparison")
        payload["calculation"]["universe"] = "autre population"
        self.assertFalse(calculation_check(payload)[0])
        payload = self.payload("vote_comparison")
        payload["observations"] *= 2
        self.assertFalse(calculation_check(payload)[0])

    def test_real_facts_are_bound_and_mutated_normalized_model_invalidates_them(self):
        document = self.make_document()
        document.source.qualified_at = timezone.now()
        document.source.terms_url = "https://www.assemblee-nationale.fr/licence"
        document.source.save()
        scrutiny = self.make_scrutiny(document)
        dossier = EditorialDossier.objects.create(stable_id="REAL:BOUND", content_kind="official_vote", established_kind="official_vote", data_scope="real")
        payload = {**official_fields(scrutiny), "coverage": self.coverage(), "themes": []}
        revision = create_revision(dossier=dossier, public_payload=payload, source_snapshot=self.snapshot(document), title="Fait lié", short_answer="", main_caveat="", reasoning="")
        record_controls(revision, deterministic_control_results(revision))
        self.assertTrue(binding_check(revision)[0])
        self.assertTrue(evaluate_revision(revision, "test").allowed)
        scrutiny.official_title = "Titre changé sous le même identifiant"
        scrutiny.save(update_fields=["official_title"])
        self.assertFalse(evaluate_revision(revision, "test").allowed)
        self.assertNotEqual(revision.public_payload["official_title"], scrutiny.official_title)

    def test_reuse_qualification_can_be_revoked_without_changing_public_payload(self):
        _, revision, document = self.make_revision()
        self.assertTrue(evaluate_revision(revision, "test").allowed)
        document.source.rights_status = Source.RightsStatus.UNKNOWN
        document.source.save()
        self.assertFalse(evaluate_revision(revision, "test").allowed)

    def test_exported_file_after_revocation_is_rejected_before_transport(self):
        _, _, _, review, event, packet = self.make_packet(speech=True)
        review.revoked = True
        review.save()
        with self.assertRaises(WordPressUnavailable), patch("atelier.services.wordpress.validate_source_url") as dns:
            self.configured_send(packet, lambda _: self.fail("transport must not execute"))
        dns.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.state, "failed")
        self.assertTrue(OutboxEvent.objects.filter(operation="suspend", state="pending").exists())

    def test_refusal_complement_and_deactivated_reviewer_neutralize_queued_content(self):
        for decision in ("reject", "request_evidence", "request_changes", "disable"):
            with self.subTest(decision=decision):
                dossier, revision, _, review, _, _ = self.make_packet(speech=True)
                if decision == "disable":
                    review.reviewer.is_active = False
                    review.reviewer.save()
                elif decision == "reject":
                    with self.final_authority_settings(review.reviewer):
                        record_human_review(
                            dossier_id=dossier.pk,
                            expected_revision=revision.revision,
                            expected_digest=revision.snapshot_digest,
                            user=review.reviewer,
                            decision=decision,
                            environment="test",
                        )
                else:
                    record_human_review(
                        dossier_id=dossier.pk,
                        expected_revision=revision.revision,
                        expected_digest=revision.snapshot_digest,
                        user=review.reviewer,
                        decision=decision,
                        environment="test",
                    )
                with tempfile.TemporaryDirectory() as tmp:
                    manifest = export_outbox(Path(tmp) / "batch", environment="test")
                self.assertFalse(any(i["record_id"] == dossier.stable_id and i["operation"] == "upsert" for i in manifest["items"]))

    def test_suspension_is_first_across_different_dossiers(self):
        self.make_packet()
        dossier, _, _, review, _, _ = self.make_packet(speech=True)
        review.revoked = True
        review.save()
        with tempfile.TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / "batch", environment="test", limit=1)
        self.assertEqual(manifest["items"][0]["operation"], "suspend")
        self.assertEqual(manifest["items"][0]["record_id"], dossier.stable_id)

    def test_same_event_id_changed_sequence_is_not_a_new_event(self):
        *_, packet = self.make_packet()
        self.assertEqual(apply_packet(packet), "applied_locally")
        changed = deepcopy(packet)
        changed["sequence"] += 1
        self.resign(changed)
        self.assertEqual(apply_packet(changed), "event_identity_conflict")

    def test_signature_payload_and_public_digest_are_separate_boundaries(self):
        *_, packet = self.make_packet()
        packet["payload"]["public"]["official_title"] = "Texte différent"
        self.assertEqual(apply_packet(packet), "unauthorized")
        self.resign(packet)
        self.assertEqual(apply_packet(packet), "snapshot_digest_mismatch")

    def test_wrong_destination_real_data_in_test_and_expired_events_are_rejected(self):
        *_, packet = self.make_packet()
        wrong = deepcopy(packet)
        wrong["destination"] = "dv-site:production"
        self.assertEqual(apply_packet(wrong), "wrong_destination_environment")
        packet["payload"]["data_scope"] = "real"
        with override_settings(DV_ENV="production"):
            self.assertEqual(apply_packet(packet), "wrong_destination_environment")
        *_, packet = self.make_packet()
        packet["expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        self.resign(packet)
        self.assertEqual(apply_packet(packet), "event_expired")

    def test_queue_can_wait_before_first_issuance_and_exact_packet_is_stable(self):
        _, revision, _ = self.make_revision()
        event = authorize_and_queue(revision, "test")
        OutboxEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=2))
        first = packet_for_event(event)
        self.assertEqual(apply_packet(first), "applied_locally")
        self.assertEqual(packet_for_event(event), first)

    def test_rotation_rejects_inactive_keys_and_preserves_active_old_key_replay(self):
        keys = {"rotation-test": {"environment": "test", "destination": "dv-site:test", "purpose": "event", "active": True, "secret": "synthetic-rotation-key-for-test-only"}}
        with override_settings(DV_EVENT_KEY_ID="rotation-test", DV_EVENT_KEYS=keys):
            *_, packet = self.make_packet()
            self.assertEqual(apply_packet(packet), "applied_locally")
            self.assertEqual(apply_packet(packet), "duplicate")
            keys["rotation-test"]["active"] = False
            self.assertEqual(apply_packet(packet), "unauthorized")

    def test_bound_receipt_is_required_and_delivery_is_not_publication(self):
        *_, event, packet = self.make_packet()
        receipt, calls = self.configured_send(packet, lambda p, **kwargs: receipt_for_packet(p, "applied"))
        self.assertEqual(calls, 1)
        self.assertEqual(receipt["event_digest"], sha256_digest(packet))
        event.refresh_from_db()
        self.assertEqual(event.state, "delivered")
        self.assertNotEqual(event.dossier.state, "published_verified")

    def test_unbound_receipt_is_not_accepted(self):
        *_, event, packet = self.make_packet()
        with self.assertRaisesMessage(WordPressUnavailable, "receipt_not_bound_to_event"):
            self.configured_send(packet, lambda *a, **k: {"status": "applied"})
        event.refresh_from_db()
        self.assertIsNone(event.delivered_at)

    def test_restored_database_marker_blocks_sending(self):
        *_, packet = self.make_packet()
        with tempfile.TemporaryDirectory() as tmp, override_settings(RUNTIME_DIR=Path(tmp)):
            (Path(tmp) / ".dv-restored-unreconciled").write_text("required", encoding="utf-8")
            with self.assertRaisesMessage(WordPressUnavailable, "restored_database_requires_receiver_reconciliation"):
                self.configured_send(packet, lambda *a, **k: self.fail("no transport"))

    def test_public_review_date_uses_project_timezone_around_midnight(self):
        dossier, revision, _ = self.make_revision("speech_vote")
        reviewer = self.make_user()
        review = self.approve(dossier, revision, reviewer)
        HumanReview.objects.filter(pk=review.pk).update(created_at=datetime(2001, 1, 1, 23, 30, tzinfo=dt_timezone.utc))
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
        self.assertEqual(event.payload["authorization"]["public_review"]["date"], "2001-01-02")

    def test_review_exposes_all_public_values_in_escaped_frozen_snapshot(self):
        dossier, revision, _ = self.make_revision()
        client = Client()
        client.force_login(self.make_user())
        response = client.get(reverse("review", args=[dossier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sha256_digest(__import__("json").loads(response.context["public_snapshot_json"])), revision.snapshot_digest)
        self.assertContains(response, "Contenu substantiel complet")
        self.assertContains(response, "expressed")
        self.assertContains(response, "result_status")
