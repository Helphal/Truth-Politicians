"""Cas voisins Q ; identités fictives, aucun serveur distant."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import override_settings
from atelier.models import OutboxEvent, RemoteExposure, VoteObservation
from atelier.services.publication import packet_for_event, queue_suspension, sign_packet
from atelier.services.reconciliation import acknowledge_withdrawal, retry_failed_withdrawal
from atelier.services.receipts import receipt_for_packet
from atelier.services.wordpress import WordPressUnavailable
from atelier.services.receiver import apply_packet
from atelier.services.canonical import sha256_digest
from atelier.services.publication import authorize_and_queue, evaluate_revision
from atelier.tests.support import AtelierTestCase
import review_followup as q


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled", DV_EVENT_KEY_ID="test-v1",
    DV_RECEIPT_KEY_ID="test-v1", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DV_REVIEW_ATTESTATION_KEY="synthetic-hardening-only-not-a-secret")
class FollowupHardeningTests(AtelierTestCase):
    speech_packet = q.FollowupTests.speech_packet
    authority_settings = q.FollowupTests.authority_settings
    reject = q.FollowupTests.reject
    pending_batch = q.FollowupTests.pending_batch
    send_simulated = q.FollowupTests.send_simulated
    make_catalogue_revision = q.FollowupTests.make_catalogue_revision

    def test_failed_suspension_retains_debt_and_exports_do_not_multiply_events(self):
        d, r, _, review, event, _ = self.speech_packet()
        self.reject(d, r, review)
        suspension = OutboxEvent.objects.get(operation="suspend")
        packet = packet_for_event(suspension)
        def lost(p, **kw):
            apply_packet(p)
            raise WordPressUnavailable("synthetic_lost_withdrawal_receipt")
        for _ in range(3):
            with self.assertRaises(WordPressUnavailable):
                self.send_simulated(packet, lost)
        for _ in range(4):
            self.pending_batch()
            queue_suspension(d, "Retrait demandé", "test")
        self.assertEqual(OutboxEvent.objects.filter(operation="suspend").count(), 1)
        debt = RemoteExposure.objects.get(event=event)
        self.assertTrue(debt.withdrawal_requested)
        self.assertIsNone(debt.resolved_at)
        self.assertEqual(debt.verified_receipt, {})
        retry_failed_withdrawal(suspension)
        self.send_simulated(packet, lambda p, **kw: receipt_for_packet(p, "duplicate"))
        debt.refresh_from_db()
        self.assertIsNotNone(debt.resolved_at)
        self.assertEqual(debt.verified_receipt["event_id"], str(suspension.event_id))
        self.pending_batch()
        self.assertEqual(OutboxEvent.objects.filter(operation="suspend").count(), 1)

    def test_unsigned_receipt_cannot_resolve(self):
        d, r, _, review, event, _ = self.speech_packet()
        self.reject(d, r, review)
        suspension = OutboxEvent.objects.get(operation="suspend")
        packet = packet_for_event(suspension)
        receipt = receipt_for_packet(packet, "applied")
        receipt["signature"] = "0" * 64
        with self.assertRaises(ValidationError):
            acknowledge_withdrawal(suspension, packet, receipt)
        self.assertIsNone(RemoteExposure.objects.get(event=event).resolved_at)

    def test_late_verified_receipt_resolves_after_delivered_legacy_status(self):
        d, r, _, review, event, _ = self.speech_packet()
        self.reject(d, r, review)
        suspension = OutboxEvent.objects.get(operation="suspend")
        packet = packet_for_event(suspension)
        OutboxEvent.objects.filter(pk=suspension.pk).update(state="delivered")
        acknowledge_withdrawal(suspension, packet, receipt_for_packet(packet, "duplicate"))
        self.assertIsNotNone(RemoteExposure.objects.get(event=event).resolved_at)

    def test_export_reconciles_failed_exposure_after_source_change(self):
        _, _, doc, _, event, _ = self.speech_packet()
        OutboxEvent.objects.filter(pk=event.pk).update(state="failed", attempts=3)
        doc.quarantined = True
        doc.save(update_fields=["quarantined"])
        self.assertEqual(self.pending_batch()["items"][0]["operation"], "suspend")

    def test_migration_backfill_recovers_failed_and_replaced_issuance(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        _, _, _, _, event, _ = self.speech_packet()
        RemoteExposure.objects.all().delete()  # Base fictive jetable seulement.
        OutboxEvent.objects.filter(pk=event.pk).update(state="failed")
        from types import SimpleNamespace
        import_module("atelier.migrations.0008_durable_remote_exposure").preserve_prior_issuance(apps, SimpleNamespace(connection=connection))
        self.assertEqual(RemoteExposure.objects.get().event_id, event.pk)

    def test_environment_debt_not_resolved_by_other_environment(self):
        d, r, _, review, event, _ = self.speech_packet()
        self.reject(d, r, review)
        other = queue_suspension(d, "Retrait privé", "private")
        self.assertNotEqual(RemoteExposure.objects.get(event=event).suspension_id, other.pk)

    def test_mandate_interval_inclusive_both_boundaries(self):
        _, _, _, scrutiny = self.make_catalogue_revision()
        vote = VoteObservation.objects.exclude(mandate=None).get()
        mandate = vote.mandate
        for start, end in ((scrutiny.occurred_on, scrutiny.occurred_on + timedelta(days=1)),
                           (scrutiny.occurred_on - timedelta(days=1), scrutiny.occurred_on)):
            mandate.starts_on, mandate.ends_on = start, end
            mandate.save()
            vote.clean()

    def test_mandate_wrong_institution_and_outside_period_rejected(self):
        _, _, _, scrutiny = self.make_catalogue_revision()
        vote = VoteObservation.objects.exclude(mandate=None).get()
        original = vote.mandate.institution
        for attribute, bad, good in (("institution", "AUTRE", original),
            ("starts_on", scrutiny.occurred_on + timedelta(days=1), vote.mandate.starts_on),
            ("ends_on", scrutiny.occurred_on - timedelta(days=1), None)):
            setattr(vote.mandate, attribute, bad)
            with self.assertRaises(ValidationError):
                vote.clean()
            setattr(vote.mandate, attribute, good)
        vote.scrutiny.date_precision = "month"
        with self.assertRaisesMessage(ValidationError, "vote_mandate_date_not_precise"):
            vote.clean()

    def test_receiver_rejects_signed_wrong_link_even_bypassing_ingestion(self):
        _, rev, _, _ = self.make_catalogue_revision()
        packet = packet_for_event(authorize_and_queue(rev, "test"))
        for mutate in (lambda m: m.update(person_id="SYN:ALPHA"),
                       lambda m: m.update(institution="AUTRE"),
                       lambda m: m.update(starts_on="2099-01-01")):
            bad = deepcopy(packet)
            mutate(bad["payload"]["public"]["catalogue"]["mandates"][0])
            bad["payload"]["authorization"]["snapshot_digest"] = sha256_digest(bad["payload"]["public"])
            bad["payload_digest"] = sha256_digest(bad["payload"])
            bad["signature"] = sign_packet({k:v for k,v in bad.items() if k not in {"signature", "signature_algorithm"}})
            self.assertNotIn(apply_packet(bad), {"applied_locally", "duplicate"})

    def test_authorization_rechecks_link_after_sql_bypass(self):
        _, rev, _, _ = self.make_catalogue_revision()
        vote = VoteObservation.objects.exclude(mandate=None).get()
        VoteObservation.objects.filter(mandate=None).update(mandate=vote.mandate)
        self.assertFalse(evaluate_revision(rev, "test").allowed)
