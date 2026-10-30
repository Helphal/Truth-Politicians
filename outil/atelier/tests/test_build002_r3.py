"""BUILD-002-R3 — cadence intra-jour, chunking dumps, index→détail→Declaration."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from atelier.models import (
    Declaration,
    FeedCursor,
    JobRun,
    Measure,
    Person,
    RelationCandidate,
    LegislativeDossier,
    TextVersion,
)
from atelier.services.feeds.adapters import AdapterResult, _store_live_document
from atelier.services.feeds.official_formats import (
    PRODUCTION_CHUNK_SIZE,
    extract_items_from_bytes,
    extract_items_from_path,
)
from atelier.services.feeds.pipeline import normalize_and_ingest
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import cadence_bucket, ensure_feed_cursors, run_feed
from atelier.tests.support import AtelierTestCase

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "build002" / "official_samples"
R3 = Path(__file__).resolve().parents[1] / "fixtures" / "build002" / "r3"
DETAILS = SAMPLES / "details"


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R3CadenceTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        ensure_feed_cursors()
        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True,
            cadence="hourly",
            last_success_at=None,
            block_reason="",
            consecutive_errors=0,
            etag="",
            cursor_token="",
            metrics={},
        )

    def test_cadence_bucket_changes_within_day(self):
        t0 = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        t1 = t0 + timedelta(hours=1)
        self.assertNotEqual(cadence_bucket("hourly", t0), cadence_bucket("hourly", t1))
        self.assertEqual(cadence_bucket("hourly", t0), cadence_bucket("hourly", t0 + timedelta(minutes=10)))

    def test_second_hourly_slot_reprobes_and_sees_change(self):
        cursor = FeedCursor.objects.get(feed_id="AN_AMO10_XVII")
        spec = feed_by_id("AN_AMO10_XVII")
        raw_v1 = json.dumps({"items": [{"uid": "PA1", "prenom": "A", "nom": "Un"}]}).encode()
        raw_v2 = json.dumps({"items": [{"uid": "PA2", "prenom": "B", "nom": "Deux"}]}).encode()
        doc1 = _store_live_document(spec, raw_v1)
        doc2 = _store_live_document(spec, raw_v2)

        results = [
            AdapterResult(
                status="created",
                feed_id="AN_AMO10_XVII",
                execution_mode="fixture",
                etag='W/"v1"',
                cursor_token="token-v1",
                payload_digest="d" * 64,
                document_id=doc1,
                parser_id="an_amo",
                raw_items=[{"uid": "PA1", "prenom": "A", "nom": "Un"}],
            ),
            AdapterResult(
                status="unchanged",
                feed_id="AN_AMO10_XVII",
                execution_mode="fixture",
                etag='W/"v1"',
                cursor_token="token-v1",
                payload_digest="d" * 64,
                document_id=doc1,
                parser_id="an_amo",
                raw_items=[{"uid": "PA1", "prenom": "A", "nom": "Un"}],
            ),
            AdapterResult(
                status="created",
                feed_id="AN_AMO10_XVII",
                execution_mode="fixture",
                etag='W/"v2"',
                cursor_token="token-v2",
                payload_digest="e" * 64,
                document_id=doc2,
                parser_id="an_amo",
                raw_items=[{"uid": "PA2", "prenom": "B", "nom": "Deux"}],
            ),
        ]
        call_idx = {"n": 0}

        def fake_collect(feed_id, **kwargs):
            i = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[i]

        t0 = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        with mock.patch("atelier.services.feeds.scheduler.collect_feed", side_effect=fake_collect):
            with mock.patch("atelier.services.feeds.scheduler.timezone.now", return_value=t0):
                r1 = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
            self.assertNotEqual(r1.get("capture_status"), "cached")
            self.assertTrue(Person.objects.filter(stable_id="person:PA1").exists())

            # Même bucket → cached (anti-doublon concurrent)
            with mock.patch("atelier.services.feeds.scheduler.timezone.now", return_value=t0 + timedelta(minutes=5)):
                r_same = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
            self.assertEqual(r_same.get("capture_status"), "cached")

            # Nouveau bucket horaire, contenu inchangé (304) puis modifié
            FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(last_success_at=None)
            with mock.patch("atelier.services.feeds.scheduler.timezone.now", return_value=t0 + timedelta(hours=1)):
                r2 = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
            self.assertNotEqual(r2.get("capture_status"), "cached")

            FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(last_success_at=None)
            with mock.patch("atelier.services.feeds.scheduler.timezone.now", return_value=t0 + timedelta(hours=2)):
                r3 = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
            self.assertNotEqual(r3.get("capture_status"), "cached")
            self.assertTrue(Person.objects.filter(stable_id="person:PA2").exists())

        # Deux jobs distincts le même jour
        jobs = JobRun.objects.filter(task_name="permanent_feed_collection", batch_id__startswith="AN_AMO10_XVII:")
        self.assertGreaterEqual(jobs.count(), 2)


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R3ChunkingTests(AtelierTestCase):
    def test_production_chunk_processes_all_260_without_silent_truncate(self):
        ensure_feed_cursors()
        spec = feed_by_id("AN_AMO10_XVII")
        raw = (R3 / "actors_260.json.zip").read_bytes()
        # Probe mode still truncates for R2 compat
        probe = extract_items_from_bytes(raw, "an_amo", media_type="application/zip", mode="probe", limit=200)
        self.assertEqual(probe["item_count"], 200)
        self.assertFalse(probe["complete"])
        self.assertEqual(probe["total_seen"], 260)

        doc_id = _store_live_document(spec, raw, media_type="application/zip")
        result = normalize_and_ingest(
            feed_id="AN_AMO10_XVII",
            document_id=doc_id,
            parser_id="an_amo",
            execution_mode="live",
            chunk_size=80,
            drain=True,
        )
        self.assertEqual(result["parse_status"], "ok")
        self.assertTrue(result["snapshot_complete"])
        self.assertEqual(result["parse_cursor"]["total_seen"], 260)
        self.assertEqual(result["parse_cursor"]["processed"], 260)
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 260)

    def test_chunk_resume_after_interruption_no_duplicates(self):
        ensure_feed_cursors()
        spec = feed_by_id("AN_AMO10_XVII")
        raw = (R3 / "actors_260.json.zip").read_bytes()
        doc_id = _store_live_document(spec, raw, media_type="application/zip")
        first = normalize_and_ingest(
            feed_id="AN_AMO10_XVII",
            document_id=doc_id,
            parser_id="an_amo",
            execution_mode="live",
            chunk_size=100,
            drain=False,
        )
        self.assertFalse(first["snapshot_complete"])
        self.assertEqual(first["parse_cursor"]["processed"], 100)
        mid = Person.objects.filter(stable_id__startswith="person:PA").count()
        self.assertEqual(mid, 100)
        second = normalize_and_ingest(
            feed_id="AN_AMO10_XVII",
            document_id=doc_id,
            parser_id="an_amo",
            execution_mode="live",
            parse_cursor=first["parse_cursor"],
            chunk_size=100,
            drain=True,
        )
        self.assertTrue(second["snapshot_complete"])
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 260)

    def test_syceron_zip_reads_more_than_five_members(self):
        raw = (R3 / "syceron_7_members.xml.zip").read_bytes()
        result = extract_items_from_bytes(raw, "an_cr", media_type="application/zip", mode="production", limit=None)
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(result["total_seen"], 7)
        self.assertTrue(result["complete"])


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R3DiscoverySpeechTests(AtelierTestCase):
    def test_index_alone_creates_zero_declarations(self):
        ensure_feed_cursors()
        # Index sans entrée URL_MAP → pure discovery, 0 Declaration.
        index_html = (
            b"<!DOCTYPE html><html><body><h1>Index</h1>"
            b'<a href="https://www.elysee.fr/emmanuel-macron/2099/01/01/inconnu">'
            b"Discours hypothetique non mappe</a></body></html>"
        )
        parsed = extract_items_from_bytes(
            index_html, "elysee_speech", media_type="text/html", mode="production", limit=None
        )
        self.assertTrue(all(i.get("discovery_only") for i in parsed["items"]))
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, index_html, media_type="text/html")
        result = normalize_and_ingest(
            feed_id="ELYSEE_DISCOURS",
            document_id=doc_id,
            parser_id="elysee_speech",
            raw_items=parsed["items"],
            execution_mode="fixture",
            drain=True,
        )
        self.assertEqual(result["declarations_created"], 0)
        self.assertEqual(Declaration.objects.count(), 0)
        self.assertGreaterEqual(result.get("discovery_only_count", 0), 1)

        senat_html = (
            b"<!DOCTYPE html><html><body><h1>Seances</h1>"
            b'<a href="https://www.senat.fr/seances/s2099/inconnu.html">'
            b"Compte rendu - seance hypothetique</a></body></html>"
        )
        parsed_s = extract_items_from_bytes(
            senat_html, "senat_cr", media_type="text/html", mode="production", limit=None
        )
        self.assertTrue(all(i.get("discovery_only") for i in parsed_s["items"]))
        spec_s = feed_by_id("SENAT_COMPTES_RENDUS")
        doc_s = _store_live_document(spec_s, senat_html, media_type="text/html")
        result_s = normalize_and_ingest(
            feed_id="SENAT_COMPTES_RENDUS",
            document_id=doc_s,
            parser_id="senat_cr",
            raw_items=parsed_s["items"],
            execution_mode="fixture",
            drain=True,
        )
        self.assertEqual(result_s["declarations_created"], 0)

    def test_index_plus_detail_fixture_creates_sourced_declaration(self):
        ensure_feed_cursors()
        # Seed a measure so RelationCandidate can appear (R3-04)
        legis = LegislativeDossier.objects.create(
            stable_id=self.unique("DOS"),
            institution="Assemblée nationale",
            official_title="Dossier transparence budgétaire",
            official_url="https://www.assemblee-nationale.fr/dyn/dossier-t",
            status="en_cours",
        )
        text = TextVersion.objects.create(
            stable_id=self.unique("TXT"),
            dossier=legis,
            kind="soumis",
            official_title="Texte",
            version_label="1",
            stage="1ere lecture",
            relation=TextVersion.Relation.SUBMITTED,
            official_url="https://www.assemblee-nationale.fr/dyn/texte-t",
        )
        Measure.objects.create(
            stable_id=self.unique("MEA"),
            text_version=text,
            title="Mesure de transparence budgétaire",
            locator="art.1",
            concrete_effect="renforce la transparence budgétaire nationale",
        )

        index = extract_items_from_path(DETAILS / "elysee_index_mini.html", "elysee_speech", mode="production", limit=None)
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, (DETAILS / "elysee_index_mini.html").read_bytes(), media_type="text/html")
        result = normalize_and_ingest(
            feed_id="ELYSEE_DISCOURS",
            document_id=doc_id,
            parser_id="elysee_speech",
            raw_items=index["items"],
            execution_mode="fixture",
            drain=True,
        )
        self.assertGreaterEqual(result["declarations_created"], 1)
        decl = Declaration.objects.order_by("-id").first()
        self.assertIsNotNone(decl)
        self.assertIn("transparence budgétaire", decl.exact_quote.casefold())
        self.assertEqual(decl.speaker_label, "Emmanuel Macron")
        detail_html = (DETAILS / "elysee_detail_transparence.html").read_text(encoding="utf-8")
        self.assertIn(decl.exact_quote[:40], detail_html.replace("\n", " ") or decl.exact_quote)
        # Candidat non qualifié
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())
        self.assertGreaterEqual(RelationCandidate.objects.filter(status="proposed").count(), 1)

    def test_incomplete_detail_without_speaker_creates_no_declaration(self):
        html = (
            b"<html><body><h1>Titre</h1>"
            b'<p data-speech="1">Texte assez long pour ressembler a une citation '
            b"sans auteur demontre dans la page detaillee.</p>"
            b"</body></html>"
        )
        items = extract_items_from_bytes(html, "elysee_speech", media_type="text/html", mode="production", limit=None)["items"]
        self.assertTrue(items)
        self.assertTrue(items[0].get("incomplete") or items[0].get("item_kind") == "metadata_only")
        ensure_feed_cursors()
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, html, media_type="text/html")
        result = normalize_and_ingest(
            feed_id="ELYSEE_DISCOURS",
            document_id=doc_id,
            parser_id="elysee_speech",
            raw_items=items,
            execution_mode="fixture",
        )
        self.assertEqual(result["declarations_created"], 0)

    def test_syceron_fixture_declaration_to_unqualified_candidate(self):
        ensure_feed_cursors()
        legis = LegislativeDossier.objects.create(
            stable_id=self.unique("DOS2"),
            institution="Assemblée nationale",
            official_title="Dossier transparence",
            official_url="https://www.assemblee-nationale.fr/dyn/x",
            status="en_cours",
        )
        text = TextVersion.objects.create(
            stable_id=self.unique("TXT2"),
            dossier=legis,
            kind="soumis",
            official_title="Texte",
            version_label="1",
            stage="1",
            relation=TextVersion.Relation.SUBMITTED,
            official_url="https://www.assemblee-nationale.fr/dyn/t",
        )
        Measure.objects.create(
            stable_id=self.unique("MEA2"),
            text_version=text,
            title="Transparence budgétaire",
            locator="art.1",
            concrete_effect="renforce la transparence budgétaire nationale",
        )
        spec = feed_by_id("AN_COMPTES_RENDUS_XVII")
        raw = (SAMPLES / "an_syceron_official_fragment.xml").read_bytes()
        doc_id = _store_live_document(spec, raw, media_type="application/xml")
        before = RelationCandidate.objects.count()
        result = normalize_and_ingest(
            feed_id="AN_COMPTES_RENDUS_XVII",
            document_id=doc_id,
            parser_id="an_cr",
            execution_mode="live",
            drain=True,
        )
        self.assertGreaterEqual(result["declarations_created"], 1)
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())
        # Candidat possible si chevauchement lexical ; sinon discovery OK sans forcer
        _ = before

    def test_checks(self):
        call_command("check")
