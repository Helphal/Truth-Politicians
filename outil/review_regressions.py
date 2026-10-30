"""Independent acceptance regressions for the received archive, offline and synthetic only.

Expected assertions express the project policy, not a claim these safeguards exist.
The received application is deliberately left unchanged. Failing assertions identify
missing safeguards. No production database, real approval, or network is used.
"""
from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import override_settings

from atelier.models import EditorialDossier, HumanReview
from atelier.tests.support import AtelierTestCase
from atelier.services.canonical import sha256_digest
from atelier.services.exporter import export_outbox
from atelier.services.publication import (
    authorize_and_queue, evaluate_revision, packet_for_event,
    queue_suspension, sign_packet,
)
from atelier.services.receiver import apply_packet
from atelier.services.wordpress import WordPressUnavailable, send_packet


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   DV_REVIEW_ATTESTATION_KEY='synthetic-test-only-key-not-a-secret')
class IndependentAcceptanceTests(AtelierTestCase):
    def packet(self):
        dossier, revision, document = self.make_revision()
        event = authorize_and_queue(revision, 'test')
        return dossier, revision, document, packet_for_event(event)

    @staticmethod
    def resign(packet):
        packet['payload_digest'] = sha256_digest(packet['payload'])
        unsigned = {k: v for k, v in packet.items()
                    if k not in {'signature_algorithm', 'signature'}}
        packet['signature'] = sign_packet(unsigned)
        return packet

    def test_R01_inconsistent_official_counts_must_block_automatic_authorization(self):
        payload = self.payload(EditorialDossier.Kind.OFFICIAL_VOTE)
        payload['counts']['for']['value'] = 999
        _, revision, _ = self.make_revision(payload=payload)
        # The fixture declares 2 expressed votes, 999 for and 1 against.
        with self.assertRaises(ValidationError):
            authorize_and_queue(revision, 'test')

    def test_R02_calculation_must_be_recomputed_from_observations(self):
        payload = self.payload(EditorialDossier.Kind.VOTE_COMPARISON)
        # One identical known pair, but a fabricated numerator and fraction.
        payload['calculation']['numerator'] = 9
        payload['calculation']['fraction']['numerator'] = 9
        _, revision, _ = self.make_revision(EditorialDossier.Kind.VOTE_COMPARISON, payload=payload)
        with self.assertRaises(ValidationError):
            authorize_and_queue(revision, 'test')

    def test_R03_revoked_approval_must_not_be_exported_from_pending_outbox(self):
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        with self.final_authority_settings(reviewer):
            review = self.approve(dossier, revision, reviewer)
            authorize_and_queue(revision, 'test')
        review.revoked = True
        review.save(update_fields=['revoked'])
        with self.final_authority_settings(reviewer):
            self.assertFalse(evaluate_revision(revision, 'test').allowed)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / 'batch', environment='test')
        self.assertEqual(manifest['count'], 0, 'Queued upsert must be cancelled or quarantined after revocation')

    def test_R04_changed_source_must_block_pending_export(self):
        dossier, revision, document = self.make_revision()
        authorize_and_queue(revision, 'test')
        self.make_document(source=document.source, captured_offset=10)
        self.assertFalse(evaluate_revision(revision, 'test').allowed)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / 'batch', environment='test')
        self.assertEqual(manifest['count'], 0, 'Stale source queued before correction must not remain exportable')

    def test_R05_suspension_must_prevent_old_upsert_even_in_bounded_export(self):
        dossier, revision, _ = self.make_revision()
        authorize_and_queue(revision, 'test')
        queue_suspension(dossier, 'Synthetic suspension regression', environment='test')
        with tempfile.TemporaryDirectory() as tmp:
            manifest = export_outbox(Path(tmp) / 'batch', environment='test', limit=1)
        self.assertEqual([i['operation'] for i in manifest['items']], ['suspend'])

    def assert_network_mode_blocks(self, mode):
        fake_connection = MagicMock()
        response = fake_connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"status":"synthetic"}'
        # All transport and DNS are mocked: this never contacts the public site.
        with override_settings(DV_NETWORK_MODE=mode, DV_PROTECTED_EGRESS_CONFIRMED=True), \
             patch.dict(os.environ, {
                 'DV_WORDPRESS_ENDPOINT': 'https://example.org/synthetic-test-only',
                 'DV_WORDPRESS_USERNAME': 'synthetic-user',
                 'DV_WORDPRESS_APP_PASSWORD': 'synthetic-not-a-secret',
             }), \
             patch('atelier.services.wordpress.validate_source_url',
                   return_value=('example.org', '/synthetic-test-only', ('192.0.2.44',))), \
             patch('atelier.services.wordpress._PinnedHTTPSConnection', return_value=fake_connection):
            blocked = False
            try:
                send_packet({'synthetic': True})
            except WordPressUnavailable:
                blocked = True
            self.assertTrue(blocked, 'Network mode must block before the transport is invoked')
            fake_connection.request.assert_not_called()

    def test_R06_wordpress_sender_must_respect_network_disabled(self):
        self.assert_network_mode_blocks('disabled')

    def test_R07_wordpress_sender_must_not_use_direct_transport_in_protected_mode(self):
        self.assert_network_mode_blocks('protected')

    def test_R08_receiver_must_verify_inner_public_digest(self):
        _, _, _, packet = self.packet()
        packet['payload']['authorization']['snapshot_digest'] = '0' * 64
        self.resign(packet)
        self.assertNotEqual(apply_packet(packet), 'applied_locally')

    def test_R09_suspension_inner_id_must_match_packet_id(self):
        dossier, _, _, _ = self.packet()
        event = queue_suspension(dossier, 'Synthetic regression', environment='test')
        packet = packet_for_event(event)
        packet['payload']['record_id'] = 'DIFFERENT:SYNTHETIC:RECORD'
        self.resign(packet)
        self.assertNotEqual(apply_packet(packet), 'applied_locally')

    def test_R10_duplicate_identity_with_changed_content_must_be_rejected(self):
        _, _, _, packet = self.packet()
        self.assertEqual(apply_packet(packet), 'applied_locally')
        changed = deepcopy(packet)
        changed['payload']['public']['official_title'] = 'Different synthetic title'
        changed['payload']['authorization']['snapshot_digest'] = sha256_digest(changed['payload']['public'])
        self.resign(changed)
        self.assertNotIn(apply_packet(changed), {'applied_locally', 'duplicate'})

    def test_R11_public_review_date_must_be_actual_review_date(self):
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        with self.final_authority_settings(reviewer):
            review = self.approve(dossier, revision, reviewer)
            HumanReview.objects.filter(pk=review.pk).update(
                created_at=datetime(2001, 1, 2, 12, 0, tzinfo=datetime_timezone.utc))
            event = authorize_and_queue(revision, 'test')
        self.assertEqual(event.payload['authorization']['public_review']['date'], '2001-01-02')

    def test_R12_production_configured_simulator_must_not_accept_synthetic_packet(self):
        _, _, _, packet = self.packet()
        with override_settings(DV_ENV='production'):
            self.assertNotEqual(apply_packet(packet), 'applied_locally')

    def test_control_A_unreviewed_comparison_is_blocked(self):
        _, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        with self.assertRaises(ValidationError):
            authorize_and_queue(revision, 'test')

    def test_control_B_invalid_signature_is_blocked(self):
        _, _, _, packet = self.packet()
        packet['signature'] = '0' * 64
        self.assertEqual(apply_packet(packet), 'unauthorized')
