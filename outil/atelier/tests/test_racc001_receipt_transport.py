"""Reçu de transport : vérification stricte et refus sans DELIVERED."""
from __future__ import annotations

import unittest

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings

from atelier.models import OutboxEvent
from atelier.services.receipts import receipt_for_packet, verify_receipt
from atelier.services.wordpress import WordPressUnavailable, send_packet
from atelier.models import Declaration
from atelier.services.publication import authorize_and_queue, packet_for_event
from atelier.services.receipts import receipt_verification_view
from atelier.services.wordpress import _extract_transport_receipt
from atelier.services.wordpress_loopback import post_packet_loopback
from atelier.tests import test_counter_review as boundary
from atelier.tests.racc001_local_b_live import LocalBLiveSupport
from atelier.tests.racc001_php import php_argv
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_racc001_corpus import Racc001CorpusTests
from atelier.tests.test_racc001_r5_local_loopback import (
    LOCAL_B_RECEIPT_KEY_ID,
    _local_b_site_confirmation_public_keys,
    _local_b_transport_keys,
)


@override_settings(
    DV_ENV="test",
    DV_NETWORK_MODE="disabled",
    DV_EVENT_KEY_ID="test-v1",
    DV_RECEIPT_KEY_ID="test-v1",
    DV_REVIEW_ATTESTATION_KEY="synthetic-counter-review-not-a-secret",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class Racc001ReceiptTransportTests(boundary.CounterReviewBoundaryTests):
    def _send_with_receipt(self, packet, receipt):
        with override_settings(
            DV_NETWORK_MODE="direct",
            DV_PUBLICATION_SEND_AUTHORIZED=True,
            DV_RECEIVER_STATE_RECONCILED=True,
        ), patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_ENDPOINT": "https://example.org/test-only",
                "DV_WORDPRESS_USERNAME": "fixture",
                "DV_WORDPRESS_APP_PASSWORD": "fixture-only",
            },
        ), patch("atelier.services.wordpress._post_packet", return_value=receipt):
            return send_packet(packet)

    def test_shared_vector_receipt_hmac_strict(self):
        vector = json.loads(
            (Path(settings.BASE_DIR) / "contracts/canonical-draft3-vectors.json").read_text(encoding="utf-8")
        )
        material = vector["test_key_material"].encode("utf-8")
        keys = {
            "synthetic-vector-event": {
                "environment": "test",
                "destination": "dv-site:test",
                "purpose": "event",
                "active": True,
                "secret": material,
            },
            "synthetic-vector-receipt": {
                "environment": "test",
                "destination": "dv-site:test",
                "purpose": "receipt",
                "active": True,
                "secret": material,
            },
        }
        with override_settings(DV_EVENT_KEY_ID="synthetic-vector-event", DV_RECEIPT_KEY_ID="synthetic-vector-receipt", DV_EVENT_KEYS=keys):
            self.assertTrue(verify_receipt(vector["receipt"], vector["event"]))

    def test_php_canonical_vector_matches_python(self):
        vector_path = Path(settings.BASE_DIR) / "contracts/canonical-draft3-vectors.json"
        php = Path(
            r"C:\Users\EXAMPLE_USER\Desktop\Truth Politicians\worktrees\dv-racc-001-site\site\tests\canonical-atelier-test.php"
        )
        if not php.is_file():
            self.skipTest("site_canonical_test_missing")
        proc = subprocess.run(
            php_argv(str(php), str(vector_path)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        tests = payload.get("tests") or {}
        self.assertTrue(tests.get("receipt_hmac_canonical_unsigned"))
        self.assertTrue(tests.get("receipt_digest_full_signed_event"))

    def test_bad_receipt_never_delivered(self):
        *_, event, packet = self.make_packet()
        good = receipt_for_packet(packet, "applied")
        cases = [
            {**good, "signature": "a" * 64},
            {**good, "key_id": "unknown-receipt-key"},
            {**good, "signature_algorithm": "HMAC-SHA512"},
            {**good, "event_digest": "0" * 64},
            {**good, "record_id": packet["record_id"] + ":tampered"},
            {**good, "destination": "dv-site:production"},
        ]
        for tampered in cases:
            event.refresh_from_db()
            event.state = OutboxEvent.State.PENDING
            event.attempts = 0
            event.save(update_fields=["state", "attempts", "updated_at"])
            with self.assertRaises(WordPressUnavailable):
                self._send_with_receipt(packet, tampered)
            event.refresh_from_db()
            self.assertNotEqual(event.state, OutboxEvent.State.DELIVERED)
            self.assertFalse(verify_receipt(tampered, packet))

    def test_omitted_receipt_fields_never_delivered(self):
        *_, event, packet = self.make_packet()
        good = receipt_for_packet(packet, "applied")
        for missing in ("signature", "signature_algorithm", "event_digest"):
            event.refresh_from_db()
            event.state = OutboxEvent.State.PENDING
            event.attempts = 0
            event.save(update_fields=["state", "attempts", "updated_at"])
            tampered = {k: v for k, v in good.items() if k != missing}
            with self.assertRaises(WordPressUnavailable):
                self._send_with_receipt(packet, tampered)
            event.refresh_from_db()
            self.assertNotEqual(event.state, OutboxEvent.State.DELIVERED)
            self.assertFalse(verify_receipt(tampered, packet))

    def test_valid_event_wrong_receipt_from_other_packet(self):
        *_, event_a, packet_a = self.make_packet()
        *_, _event_b, packet_b = self.make_packet()
        foreign = receipt_for_packet(packet_b, "applied")
        event_a.refresh_from_db()
        event_a.state = OutboxEvent.State.PENDING
        event_a.attempts = 0
        event_a.save(update_fields=["state", "attempts", "updated_at"])
        with self.assertRaises(WordPressUnavailable):
            self._send_with_receipt(packet_a, foreign)
        event_a.refresh_from_db()
        self.assertNotEqual(event_a.state, OutboxEvent.State.DELIVERED)


class _CorpusHarness(Racc001CorpusTests):
    __test__ = False


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
@unittest.skipUnless(os.environ.get("DV_RACC001_LIVE_WORDPRESS") == "1",
    "Integration WordPress Local B non demandee : banc reel et cles SYNTH requis")
class Racc001ReceiptLiveWpInteropTests(LocalBLiveSupport, AtelierTestCase):
    def setUp(self):
        AtelierTestCase.setUp(self)
        self._corpus = _CorpusHarness()
        self._corpus.setUp()
        self._corpus.unique = self.unique

    def unique(self, prefix: str) -> str:
        import uuid

        return f"{prefix}:{uuid.uuid4().hex[:12]}"

    def _live_settings(self, keys, holder):
        return override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_transport_keys(),
            DV_RECEIPT_KEY_ID=LOCAL_B_RECEIPT_KEY_ID,
            DV_SITE_PUBLICATION_PUBLIC_KEYS=_local_b_site_confirmation_public_keys(),
        )

    def test_wp_receipt_strict_hmac_interop(self):
        declaration, _, scrutiny, _ = self._corpus.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._corpus._revision_with_comparison(declaration, scrutiny)
        user = self._corpus.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with self._loopback_patch(), self._live_settings(keys, holder):
            event = authorize_and_queue(revision, "test")
            self._post_review_wp(dossier, revision, event)
            packet = packet_for_event(event)
            raw = post_packet_loopback(packet)
            receipt = _extract_transport_receipt(raw)
            if not verify_receipt(receipt, packet):
                body = receipt_verification_view(receipt)
                expected = receipt_for_packet(packet, body["status"])
                diffs = {
                    k: {"wp": body.get(k), "py": expected.get(k)}
                    for k in sorted(set(body) | set(expected))
                    if body.get(k) != expected.get(k)
                }
                self.fail(f"live_receipt_strict_verify_failed diffs={diffs}")
