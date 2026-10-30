"""Recette BUILD-002 — feeds, speech, ingest, explain, storage (réseau désactivé)."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from atelier.models import (
    AuditEvent,
    BlobReference,
    BlobTombstone,
    Declaration,
    DocumentaryQualification,
    FeedCursor,
    Measure,
    Person,
    RelationCandidate,
    Source,
    StoredBlob,
)
from atelier.services.explain import seed_explain_entries, latest_entries
from atelier.services.feeds import all_feed_specs, coverage_matrix, ensure_feed_cursors, run_cadence_once
from atelier.services.feeds.adapters import collect_feed
from atelier.services.ingest import (
    ingest_speech_batch,
    nocturnal_reconcile,
    promote_candidate_requires_human,
    propose_measure_candidates,
)
from atelier.services.speech import (
    BlockedSocialConnector,
    BlueskyPublicConnector,
    FixtureTranscriptionEngine,
    InstitutionalDebateConnector,
    NullTranscriptionEngine,
    PressRssConnector,
    SpeechCandidate,
    YoutubeMetaConnector,
    persist_speech_candidate,
    resolve_identity,
)
from atelier.services.storage_lifecycle import (
    attach_blob,
    garbage_collect_heavy,
    mark_dossier_rejected,
    reactivate_reference,
    register_blob,
    storage_metrics,
)
from atelier.services.thematic_qualification import record_declaration_measure_link
from atelier.tests.support import AtelierTestCase


@override_settings(DV_NETWORK_MODE="disabled")
class Build002FeedsTests(AtelierTestCase):
    def test_registry_covers_an_senat_legifrance_speech_social(self):
        ids = {s.feed_id for s in all_feed_specs()}
        self.assertIn("AN_VOTES_XVII", ids)
        self.assertIn("AN_AMENDEMENTS_XVII", ids)
        self.assertIn("SENAT_DOSLEG", ids)
        self.assertIn("SENAT_AMELI", ids)
        self.assertIn("LEGIFRANCE_JORF_PISTE", ids)
        self.assertIn("ELYSEE_DISCOURS", ids)
        self.assertIn("X_TWITTER", ids)
        self.assertIn("BLUESKY_PUBLIC", ids)

    def test_ensure_cursors_and_legifrance_blocked(self):
        ensure_feed_cursors()
        legi = FeedCursor.objects.get(feed_id="LEGIFRANCE_JORF_PISTE")
        self.assertEqual(legi.block_reason, "blocked_credentials_or_provider_terms")
        self.assertFalse(legi.ingestion_active)
        result = collect_feed("LEGIFRANCE_JORF_PISTE")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error_code, "contract_only")
        self.assertFalse(result.detail.get("ready_when_credentials_injected"))

    def test_fixture_collect_idempotent_etag(self):
        ensure_feed_cursors()
        first = collect_feed("AN_VOTES_XVII")
        self.assertIn(first.status, {"fixture", "created", "unchanged"})
        self.assertTrue(first.etag)
        second = collect_feed("AN_VOTES_XVII", etag=first.etag)
        self.assertEqual(second.status, "unchanged")

    def test_scheduler_once_network_disabled(self):
        ensure_feed_cursors()
        # Activer un flux daily pour le test
        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True, last_success_at=None, block_reason=""
        )
        result = run_cadence_once(["daily"], use_lock=False)
        self.assertIsNotNone(result.get("results"))
        matrix = coverage_matrix()
        self.assertTrue(any(r["feed_id"] == "AN_AMO10_XVII" for r in matrix))


@override_settings(DV_NETWORK_MODE="disabled")
class Build002SpeechTests(AtelierTestCase):
    def test_institutional_persist_and_homonym_conflict(self):
        source = self.make_document().source
        Person.objects.create(stable_id="p1", display_name="Dupont Alice", identity_status="resolved")
        Person.objects.create(stable_id="p2", display_name="Dupont Alice", identity_status="resolved")
        person, org, status = resolve_identity(display_name="Dupont Alice")
        self.assertIsNone(person)
        self.assertEqual(status, "conflict")
        connector = InstitutionalDebateConnector(
            [
                {
                    "stable_id": "decl:test:1",
                    "speaker_label": "Orateur Test",
                    "capacity": "député",
                    "exact_quote": "Je soutiens cette mesure de transparence budgétaire.",
                    "occurred_on": "2024-01-30",
                    "source_url": "https://www.assemblee-nationale.fr/dyn/test",
                    "locator": "p.1",
                    "person_stable_id": "PA999",
                }
            ]
        )
        decl = persist_speech_candidate(connector.discover()[0], source=source)
        self.assertEqual(decl.verification_status, "institutional_candidate")
        self.assertEqual(decl.source_role, Declaration.SourceRole.ORIGINAL)

    def test_press_relay_not_silently_original(self):
        source = self.make_document().source
        cands = PressRssConnector(
            [
                {
                    "stable_id": "decl:press:1",
                    "speaker_label": "Ministre X",
                    "exact_quote": "Citation rapportée.",
                    "occurred_on": "2024-02-01",
                    "source_url": "https://www.example.invalid/article",
                    "source_role": "relay",
                    "genre": "reported_quote",
                }
            ]
        ).discover()
        decl = persist_speech_candidate(cands[0], source=source)
        self.assertEqual(decl.source_role, Declaration.SourceRole.RELAY)

    def test_youtube_no_full_video_and_blocked_social(self):
        cands = YoutubeMetaConnector(
            [
                {
                    "stable_id": "decl:yt:1",
                    "speaker_label": "Chaîne officielle",
                    "title": "Discours",
                    "occurred_on": "2024-03-01",
                    "video_id": "abc",
                    "source_url": "https://www.youtube.com/watch?v=abc",
                }
            ]
        ).discover()
        self.assertTrue(cands[0].extra.get("transcript_absent"))
        self.assertFalse(cands[0].extra.get("full_video_retained"))
        health = BlockedSocialConnector("x").health()
        self.assertEqual(health["status"], "blocked_credentials_or_provider_terms")
        self.assertFalse(health["account_created"])
        self.assertIsNone(NullTranscriptionEngine().transcribe("x"))
        self.assertEqual(FixtureTranscriptionEngine({"a": "bonjour"}).transcribe("a"), "bonjour")
        self.assertEqual(len(BlueskyPublicConnector([]).discover()), 0)


@override_settings(DV_NETWORK_MODE="disabled")
class Build002IngestTests(AtelierTestCase):
    def test_dedupe_and_candidates_without_auto_qualify(self):
        source = self.make_document().source
        dossier = self.make_dossier_bundle() if hasattr(self, "make_dossier_bundle") else None
        # Create a measure via support helpers if available
        from atelier.models import LegislativeDossier, TextVersion

        legis = LegislativeDossier.objects.create(
            stable_id=self.unique("DOS"),
            institution="Assemblée nationale",
            official_title="Dossier test transparence budgétaire",
            official_url="https://www.assemblee-nationale.fr/dyn/dossier-test",
            status="en_cours",
        )
        text = TextVersion.objects.create(
            stable_id=self.unique("TXT"),
            dossier=legis,
            kind="soumis",
            official_title="Texte test",
            version_label="1",
            stage="1ere lecture",
            relation=TextVersion.Relation.SUBMITTED,
            official_url="https://www.assemblee-nationale.fr/dyn/texte-test",
        )
        measure = Measure.objects.create(
            stable_id=self.unique("MEA"),
            text_version=text,
            title="Mesure de transparence budgétaire nationale",
            locator="art.1",
            concrete_effect="renforce la transparence budgétaire",
        )
        cand = SpeechCandidate(
            stable_id="decl:ingest:1",
            speaker_label="Orateur",
            capacity="député",
            exact_quote="Je veux plus de transparence budgétaire nationale maintenant.",
            occurred_on=date(2024, 1, 1),
            source_url="https://www.assemblee-nationale.fr/dyn/a",
            locator="1",
            person_stable_id="PA1",
        )
        result = ingest_speech_batch([cand, cand], source=source)
        self.assertEqual(result["created"], 1)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertTrue(RelationCandidate.objects.filter(right_object_id=measure.stable_id).exists())
        rc = RelationCandidate.objects.first()
        with self.assertRaises(PermissionError):
            promote_candidate_requires_human(rc)
        # Aucun lien qualifié automatique
        self.assertFalse(
            Declaration.objects.get(stable_id="decl:ingest:1").measure_links.filter(
                qualification_status=DocumentaryQualification.QUALIFIED
            ).exists()
        )

    def test_nocturnal_fp3_opens_candidate_revision(self):
        from atelier.models import EvidencePassage

        doc = self.make_document()
        passage = EvidencePassage.objects.create(
            document=doc,
            exact_text="Citation initiale sur la transparence.",
            locator="p1",
            public_url="https://www.assemblee-nationale.fr/dyn/x",
        )
        person = Person.objects.create(stable_id=self.unique("P"), display_name="Test")
        decl = Declaration.objects.create(
            stable_id=self.unique("D"),
            speaker_person=person,
            speaker_label="Test",
            capacity="député",
            exact_quote="Citation initiale sur la transparence.",
            occurred_on=date(2024, 1, 1),
            source_passage=passage,
            position_scope=Declaration.PositionScope.PERSONAL,
        )
        from atelier.models import LegislativeDossier, TextVersion

        legis = LegislativeDossier.objects.create(
            stable_id=self.unique("DOS"),
            institution="AN",
            official_title="D",
            official_url="https://www.assemblee-nationale.fr/dyn/d",
            status="x",
        )
        text = TextVersion.objects.create(
            stable_id=self.unique("TXT"),
            dossier=legis,
            kind="soumis",
            official_title="T",
            version_label="1",
            stage="1ere lecture",
            relation=TextVersion.Relation.SUBMITTED,
            official_url="https://www.assemblee-nationale.fr/dyn/t",
        )
        measure = Measure.objects.create(
            stable_id=self.unique("M"),
            text_version=text,
            title="Transparence",
            locator="a1",
            concrete_effect="effet",
        )
        link = record_declaration_measure_link(
            user=None,
            declaration=decl,
            measure=measure,
            stance="support",
            qualification_status=DocumentaryQualification.QUALIFIED,
            evidence=passage,
            rationale="qualification initiale de test",
        )
        # Mutation substantielle de la citation → empreinte périmée
        decl.exact_quote = "Citation entièrement modifiée après coup."
        decl.save(update_fields=["exact_quote", "updated_at"])
        result = nocturnal_reconcile()
        self.assertGreaterEqual(result["stale"], 1)
        latest = decl.measure_links.order_by("-revision").first()
        self.assertEqual(latest.qualification_status, DocumentaryQualification.CANDIDATE)
        self.assertGreater(latest.revision, link.revision)


@override_settings(DV_NETWORK_MODE="disabled")
class Build002ExplainStorageTests(AtelierTestCase):
    def test_explain_seed_and_api(self):
        from atelier.services.explain import public_projection

        seed_explain_entries()
        entries = latest_entries()
        self.assertGreaterEqual(len(entries), 8)
        self.assertTrue(any(e.stable_id == "explain.procedure.scrutin" for e in entries))
        for e in entries:
            self.assertTrue(e.official_sources)
            lowered = f"{e.short_text} {e.detailed_text}".casefold()
            self.assertNotIn("votez pour", lowered)
            self.assertNotIn("meilleur parti", lowered)
        client = Client()
        response = client.get("/api/explain/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["partisan_commentary"])
        public = public_projection(authorization_state="authorized")
        self.assertEqual(public["format"], "dv.explain-public/0.1.0")
        self.assertEqual(public["authorization_state"], "authorized")
        self.assertFalse(public["partisan_commentary"])
        self.assertGreaterEqual(len(public["entries"]), 8)
        self.assertTrue(all(e.get("authorization_state") == "authorized" for e in public["entries"]))
        draft = public_projection(authorization_state="draft")
        self.assertEqual(draft["entries"], [])

    def test_storage_shared_ref_reject_reactivate_gc_dry_run(self):
        blob = register_blob(
            content=b"x" * (6 * 1024 * 1024),
            media_type="video/mp4",
            source_url="https://www.elysee.fr/video/test",
            metadata={"useful_excerpt": "extrait utile", "timecodes": ["00:01"], "provenance": {"fixture": True}},
        )
        self.assertEqual(blob.retention_class, StoredBlob.RetentionClass.HEAVY)
        attach_blob(blob, object_type="EditorialDossier", object_id="d1")
        attach_blob(blob, object_type="EditorialDossier", object_id="d2")
        mark_dossier_rejected("EditorialDossier", "d1", reason="rejet éditorial final")
        # Toujours référencé par d2 → pas éligible
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.ACTIVE)
        mark_dossier_rejected("EditorialDossier", "d2", reason="rejet éditorial final")
        # Rejet « maintenant » d'une référence ancienne : updated_at peut rester vieux,
        # rejected_at doit ancrer la quarantaine (pas d'éligibilité immédiate).
        BlobReference.objects.filter(blob=blob).update(updated_at=timezone.now() - timedelta(days=30))
        from atelier.services import storage_lifecycle as sl

        sl._refresh_purge_eligibility()
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.QUARANTINE)
        # Quarantaine écoulée : forcer rejected_at métier, jamais updated_at seul
        BlobReference.objects.filter(blob=blob).update(rejected_at=timezone.now() - timedelta(days=30))
        sl._refresh_purge_eligibility()
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.PURGE_ELIGIBLE)
        dry = garbage_collect_heavy(dry_run=True, explicit=False)
        self.assertTrue(dry.dry_run)
        self.assertGreaterEqual(dry.eligible, 1)
        self.assertEqual(dry.purged, 0)
        self.assertTrue(AuditEvent.objects.filter(event_type="blob_gc_dry_run").exists())
        # Réactivation empêche purge
        reactivate_reference("EditorialDossier", "d2")
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.ACTIVE)
        metrics = storage_metrics()
        self.assertTrue(metrics["no_political_scores"])
        # Light text conservé
        light = register_blob(content=b'{"ok":true}', media_type="application/json")
        self.assertEqual(light.retention_class, StoredBlob.RetentionClass.LIGHT)

    def test_reject_old_reference_now_stays_quarantine(self):
        """C04 : rejet maintenant d'une référence créée il y a longtemps ≠ purge immédiate."""
        blob = register_blob(
            content=b"y" * (6 * 1024 * 1024),
            media_type="video/mp4",
            source_url="https://www.elysee.fr/video/test-c04",
        )
        ref = attach_blob(blob, object_type="EditorialDossier", object_id="old-d")
        BlobReference.objects.filter(pk=ref.pk).update(updated_at=timezone.now() - timedelta(days=90))
        mark_dossier_rejected("EditorialDossier", "old-d", reason="rejet actuel")
        ref.refresh_from_db()
        self.assertIsNotNone(ref.rejected_at)
        self.assertGreater(ref.rejected_at, timezone.now() - timedelta(minutes=5))
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.QUARANTINE)
        # Historique REJECTED sans rejected_at : pas d'éligibilité inventée
        BlobReference.objects.filter(pk=ref.pk).update(rejected_at=None)
        from atelier.services import storage_lifecycle as sl

        sl._refresh_purge_eligibility()
        blob.refresh_from_db()
        self.assertEqual(blob.lifecycle, StoredBlob.Lifecycle.QUARANTINE)

    def test_seed_command_and_coverage_api(self):
        call_command("seed_build002")
        self.assertTrue(FeedCursor.objects.filter(feed_id="SENAT_AKN_DEPOTS").exists())
        client = Client()
        response = client.get("/api/feeds/coverage/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["no_political_scores"])
        health = client.get("/sante/")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertIn("feeds_freshness", body)
        self.assertTrue(body["no_political_performance_metrics"])
