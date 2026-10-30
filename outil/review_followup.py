"""Contre-revue r3. Données et identités fictives, transport simulé seulement.
Exécution: manage.py test review_followup (ce fichier placé sur PYTHONPATH).
Les assertions expriment les protections attendues, pas le comportement défectueux.
"""
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from atelier.models import (EditorialDossier, OutboxEvent, ReceiverCursor,
    Person, Mandate, VoteObservation, HumanReview)
from atelier.services.publication import (authorize_and_queue, packet_for_event,
    record_human_review, reissue_expired_event)
from atelier.services.exporter import export_outbox
from atelier.services.wordpress import send_packet, WordPressUnavailable
from atelier.services.receipts import receipt_for_packet
from atelier.services.receiver import apply_packet
from atelier.services.catalogue import official_fields, build_catalogue
from atelier.services.revisions import create_revision, record_controls, deterministic_control_results
from atelier.tests.support import AtelierTestCase


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled", DV_EVENT_KEY_ID="test-v1",
    DV_RECEIPT_KEY_ID="test-v1", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DV_REVIEW_ATTESTATION_KEY="synthetic-review-followup-not-a-real-secret")
class FollowupTests(AtelierTestCase):
    def speech_packet(self):
        dossier, revision, doc = self.make_revision("speech_vote")
        user = self.make_user()
        self._final_authority_user = user
        # L'autorité configurée doit rester active à l'autorisation, au paquet,
        # à l'envoi et à la revalidation (binding RACC-001 : vide = refus).
        with self.final_authority_settings(user):
            review = record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=user,
                decision=HumanReview.Decision.APPROVE,
                environment="test",
            )
            event = authorize_and_queue(revision, "test")
            packet = packet_for_event(event)
        return dossier, revision, doc, review, event, packet

    def authority_settings(self):
        user = getattr(self, "_final_authority_user", None)
        if user is None:
            raise AssertionError("final_authority_user_missing_call_speech_packet_first")
        return self.final_authority_settings(user)

    def reject(self, dossier, revision, review):
        with self.final_authority_settings(review.reviewer):
            record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=review.reviewer,
                decision=HumanReview.Decision.REJECT,
                environment="test",
            )

    def pending_batch(self):
        with self.authority_settings(), TemporaryDirectory() as tmp:
            return export_outbox(Path(tmp) / "batch", environment="test")

    def send_simulated(self, packet, action):
        with self.authority_settings(), override_settings(
            DV_NETWORK_MODE="direct",
            DV_PUBLICATION_SEND_AUTHORIZED=True,
            DV_RECEIVER_STATE_RECONCILED=True,
        ), patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_ENDPOINT": "https://example.org/fictitious-test-only",
                "DV_WORDPRESS_USERNAME": "synthetic-account",
                "DV_WORDPRESS_APP_PASSWORD": "synthetic-only",
            },
        ), patch("atelier.services.wordpress._post_packet", side_effect=action):
            return send_packet(packet)

    def test_Q01_receiver_applied_but_receipts_lost_then_rejection_still_suspends(self):
        dossier, rev, doc, review, event, packet = self.speech_packet()
        def lost_response(p, **kw):
            self.assertIn(apply_packet(p), {"applied_locally", "duplicate"})
            raise WordPressUnavailable("synthetic_receipt_lost_after_receiver_commit")
        for _ in range(3):
            with self.assertRaises(WordPressUnavailable):
                self.send_simulated(packet, lost_response)
        event.refresh_from_db()
        self.assertEqual(event.state, OutboxEvent.State.FAILED)
        cursor = ReceiverCursor.objects.get(record_id=dossier.stable_id)
        self.assertTrue(cursor.active_payload)
        self.assertFalse(cursor.suspended)
        self.reject(dossier, rev, review)
        batch = self.pending_batch()
        self.assertTrue(any(i["operation"] == "suspend" and i["record_id"] == dossier.stable_id
                            for i in batch["items"]),
            "Après trois reçus perdus, l'upsert peut être actif chez le récepteur : le refus doit produire une suspension.")

    def test_Q02_expired_upsert_superseded_before_reissue_export_then_rejection_suspends(self):
        dossier, rev, doc, review, event, packet = self.speech_packet()
        self.assertEqual(apply_packet(packet), "applied_locally")
        event.refresh_from_db()
        later = event.issued_at + timedelta(seconds=301)
        with patch("django.utils.timezone.now", return_value=later), self.authority_settings():
            replacement = reissue_expired_event(event)
            self.assertIsNone(replacement.issued_at)
            self.reject(dossier, rev, review)
            batch = self.pending_batch()
        self.assertTrue(any(i["operation"] == "suspend" and i["record_id"] == dossier.stable_id
                            for i in batch["items"]),
            "La copie ancienne reçue ne disparaît pas à l'expiration ; sa réémission non envoyée ne doit pas effacer la dette de suspension.")

    def make_catalogue_revision(self, *, wrong_mandate=False):
        doc = self.make_document()
        doc.source.qualified_at=timezone.now()
        doc.source.terms_url="https://www.assemblee-nationale.fr/synthetic-terms"
        doc.source.save()
        scrutiny=self.make_scrutiny(doc)
        alice=Person.objects.create(stable_id="SYN:ALPHA", display_name="Personne fictive Alpha")
        beta=Person.objects.create(stable_id="SYN:BETA", display_name="Personne fictive Bêta")
        mandate=Mandate.objects.create(stable_id="SYN:MANDATE:BETA", person=beta,
            institution=scrutiny.institution, mandate_type="mandat fictif", territory="territoire fictif",
            starts_on=date(2020,1,1), source_document=doc)
        for person, category in ((alice,"for"),(beta,"against")):
            observation=VoteObservation(scrutiny=scrutiny, person=person,
                mandate=(mandate if person==beta or wrong_mandate else None),
                source_category=category, normalized_category=category,
                mapping_rule="SYNTHETIC_MAPPING", source_document=doc)
            observation.full_clean()
            observation.save()
        # Le circuit `real` est exercé avec des données entièrement fictives.
        # Aucun fait politique réel ni destinataire distant n'est utilisé.
        dossier=EditorialDossier.objects.create(stable_id="SYN:REAL-PIPELINE",
            content_kind="official_vote", established_kind="official_vote", data_scope="real")
        rev=create_revision(dossier=dossier, public_payload={**official_fields(scrutiny),
            "coverage":self.coverage(), "themes":[]}, source_snapshot=self.snapshot(doc),
            title="Test fictif d'identités", short_answer="", main_caveat="", reasoning="")
        record_controls(rev, deterministic_control_results(rev))
        return dossier,rev,doc,scrutiny

    def test_Q03_wrong_person_mandate_must_not_pass_automatic_publication(self):
        try:
            dossier,rev,*_=self.make_catalogue_revision(wrong_mandate=True)
            packet=packet_for_event(authorize_and_queue(rev,"test"))
        except ValidationError:
            return  # Refus producteur admissible.
        outcome=apply_packet(packet)
        self.assertNotEqual(outcome,"applied_locally",
            "Le vote d'Alpha référence le mandat de Bêta : catalogue fermé ne signifie pas identité cohérente.")

    def test_Q04_current_successful_delivery_then_rejection_emits_suspension(self):
        dossier,rev,doc,review,event,packet=self.speech_packet()
        def success(p, **kw):
            self.assertIn(apply_packet(p), {"applied_locally","duplicate"})
            return receipt_for_packet(p,"applied")
        self.send_simulated(packet,success)
        self.reject(dossier,rev,review)
        batch=self.pending_batch()
        self.assertTrue(any(i["operation"]=="suspend" for i in batch["items"]))

    def test_Q05_correct_person_mandate_remains_eligible(self):
        _,rev,*_=self.make_catalogue_revision(wrong_mandate=False)
        self.assertEqual(apply_packet(packet_for_event(authorize_and_queue(rev,"test"))),"applied_locally")

    def test_Q06_251_observations_refused_without_silent_truncation(self):
        _,rev,doc,scrutiny=self.make_catalogue_revision()
        for i in range(249):
            p=Person.objects.create(stable_id=f"SYN:EXTRA:{i:03}",display_name=f"Fictif {i}")
            VoteObservation.objects.create(scrutiny=scrutiny,person=p,source_category="unknown",
                normalized_category="unknown",mapping_rule="SYNTHETIC_MAPPING",source_document=doc)
        with self.assertRaisesMessage(ValidationError,"catalogue_batch_too_large_split_scope"):
            build_catalogue([scrutiny.stable_id])
