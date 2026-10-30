"""BUILD-002-R4 — detail_fetch live + drainage snapshots hors cadence réseau."""
from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from atelier.models import (
    Declaration,
    DocumentVersion,
    FeedCursor,
    LegislativeDossier,
    Measure,
    Person,
    RelationCandidate,
    TextVersion,
)
from atelier.services.feeds.adapters import AdapterResult, _store_live_document
from atelier.services.feeds.detail_fetch import DetailFetchResult, lookup_detail_fixture, resolve_detail
from atelier.services.feeds.pipeline import normalize_and_ingest
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import (
    continue_ingest,
    due_feeds,
    ensure_feed_cursors,
    run_feed,
)
from atelier.tests.support import AtelierTestCase

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "build002" / "official_samples"
DETAILS = SAMPLES / "details"


def _actors_zip(n: int) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1, n + 1):
            payload = {
                "acteur": {
                    "uid": {"#text": f"PA{i:06d}"},
                    "etatCivil": {"ident": {"prenom": f"P{i}", "nom": f"N{i}"}},
                }
            }
            zf.writestr(f"json/acteur/PA{i:06d}.json", json.dumps(payload, ensure_ascii=False))
    return buf.getvalue()


DETAIL_HTML_OK = (
    b'<!DOCTYPE html><html><body><h1>Discours test</h1>'
    b'<div data-author="Emmanuel Macron">'
    b'<p data-speech="1">Nous devons renforcer la transparence budgetaire nationale dans chaque debat public.</p>'
    b'<p data-speech="1">Cette exigence de clarte budgetaire engage la responsabilite de l executif devant la nation.</p>'
    b"</div></body></html>"
)

DETAIL_HTML_NO_AUTHOR = (
    b'<!DOCTYPE html><html><body><h1>Sans auteur</h1>'
    b'<p data-speech="1">Texte suffisamment long pour un paragraphe mais sans auteur demontre dans la page.</p>'
    b'<p data-speech="1">Deuxieme paragraphe pour depasser le seuil de quarante caracteres utiles.</p>'
    b"</body></html>"
)


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R4DetailFetchTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        ensure_feed_cursors()

    def test_simulated_live_index_fetches_detail_and_creates_declaration(self):
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
        index_html = (
            b"<!DOCTYPE html><html><body>"
            b'<a href="https://www.elysee.fr/emmanuel-macron/2024/03/15/transparence">'
            b"Discours sur la transparence budgetaire</a></body></html>"
        )
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, index_html, media_type="text/html")
        parent = DocumentVersion.objects.get(pk=doc_id)
        fetch_calls = []

        def fake_resolve(url, **kwargs):
            fetch_calls.append(url)
            self.assertEqual(kwargs.get("execution_mode"), "live")
            stored = _store_live_document(
                spec,
                DETAIL_HTML_OK,
                media_type="text/html",
                provenance={"stage": "detail_fetch", "parent_document_id": parent.pk},
            )
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            result = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        self.assertEqual(len(fetch_calls), 1)
        self.assertEqual(result["details_fetched"], 1)
        self.assertEqual(result["details_parsed"], 1)
        self.assertEqual(result["declarations_created"], 1)
        self.assertEqual(Declaration.objects.count(), 1)
        decl = Declaration.objects.get()
        self.assertIn("transparence", decl.exact_quote.lower())
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())
        self.assertGreaterEqual(RelationCandidate.objects.filter(status="proposed").count(), 1)
        detail_docs = DocumentVersion.objects.exclude(pk=doc_id)
        self.assertGreaterEqual(detail_docs.count(), 1)
        self.assertEqual(decl.source_passage.document_id, detail_docs.first().pk)

    def test_detail_without_author_creates_zero_declarations(self):
        index_html = (
            b"<!DOCTYPE html><html><body>"
            b'<a href="https://www.elysee.fr/emmanuel-macron/2024/01/01/sans-auteur">'
            b"Allocution hypothetique sans auteur</a></body></html>"
        )
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, index_html, media_type="text/html")

        def fake_resolve(url, **kwargs):
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=None,
                raw=DETAIL_HTML_NO_AUTHOR,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            result = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        self.assertEqual(result["declarations_created"], 0)
        self.assertEqual(Declaration.objects.count(), 0)
        self.assertGreaterEqual(result.get("incomplete_count", 0) + result.get("discovery_only_count", 0), 1)

    def test_host_not_allowed_blocks_explicitly(self):
        result = resolve_detail(
            "https://evil.example/page",
            feed_id="ELYSEE_DISCOURS",
            parser_id="elysee_speech",
            parent_document=None,
            execution_mode="live",
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn(result.error_code, {"host_not_allowed", "fixture_lookup_forbidden_in_live", "network_blocked"})

    @override_settings(DV_NETWORK_MODE="direct")
    def test_redirect_to_foreign_host_blocked(self):
        from atelier.services.collector import UnsafeDestination

        def boom(*args, **kwargs):
            raise UnsafeDestination("redirect_host_not_allowed")

        with mock.patch("atelier.services.feeds.detail_fetch._http_get_bounded", side_effect=boom):
            result = resolve_detail(
                "https://www.elysee.fr/emmanuel-macron/2024/03/15/transparence",
                feed_id="ELYSEE_DISCOURS",
                parser_id="elysee_speech",
                parent_document=None,
                execution_mode="live",
            )
        self.assertEqual(result.status, "blocked")
        self.assertIn(result.error_code, {"redirect_forbidden", "host_not_allowed"})

    def test_unchanged_detail_no_duplicate_declaration(self):
        index_html = (
            b"<!DOCTYPE html><html><body>"
            b'<a href="https://www.elysee.fr/emmanuel-macron/2024/03/15/transparence">'
            b"Discours sur la transparence budgetaire</a></body></html>"
        )
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, index_html, media_type="text/html")
        detail_id = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")

        def fake_resolve(url, **kwargs):
            return DetailFetchResult(
                status="unchanged",
                url=url,
                document_id=detail_id,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            r1 = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
            r2 = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        self.assertEqual(r1["details_unchanged"], 1)
        # R5 : item déjà terminal → pas de re-fetch ; idempotence Declaration.
        self.assertEqual(r2.get("details_unchanged", 0), 0)
        self.assertEqual(Declaration.objects.count(), 1)

    @override_settings(DV_NETWORK_MODE="direct")
    def test_fixture_lookup_forbidden_in_live_mode(self):
        url = "https://www.elysee.fr/emmanuel-macron/2024/03/15/transparence"
        self.assertIsNotNone(lookup_detail_fixture("elysee_speech", url))
        with mock.patch(
            "atelier.services.feeds.detail_fetch._http_get_bounded",
            side_effect=AssertionError("fixture must not be used as live fallback"),
        ):
            # Sans mock HTTP réussi : live n'utilise jamais lookup_detail_fixture
            # (on vérifie via resolve_detail quand network échoue → failed, pas fixture)
            with mock.patch(
                "atelier.services.feeds.detail_fetch._http_get_bounded",
                side_effect=OSError("simulated"),
            ):
                result = resolve_detail(
                    url,
                    feed_id="ELYSEE_DISCOURS",
                    parser_id="elysee_speech",
                    parent_document=None,
                    execution_mode="live",
                )
        self.assertNotEqual(result.status, "fixture")
        self.assertIn(result.status, {"failed", "blocked"})


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R4DrainTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        ensure_feed_cursors()

    def test_1200_objects_drained_without_daily_cadence_wait(self):
        spec = feed_by_id("AN_AMO10_XVII")
        raw = _actors_zip(1200)
        doc_id = _store_live_document(spec, raw, media_type="application/zip")
        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True,
            cadence="daily",
            last_success_at=timezone.now(),  # cadence quotidienne « pleine »
            metrics={},
            consecutive_errors=0,
            block_reason="",
        )
        result = AdapterResult(
            status="created",
            feed_id="AN_AMO10_XVII",
            execution_mode="live",
            etag='W/"1200"',
            cursor_token="tok-1200",
            payload_digest="a" * 64,
            document_id=doc_id,
            parser_id="an_amo",
        )
        with mock.patch("atelier.services.feeds.scheduler.collect_feed", return_value=result):
            # Forcer hors continuation pour exercer le drain post-capture
            FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(metrics={}, last_success_at=None)
            out = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
        self.assertTrue(out.get("snapshot_complete"))
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 1200)
        cursor = FeedCursor.objects.get(feed_id="AN_AMO10_XVII")
        self.assertTrue(cursor.metrics.get("snapshot_complete"))
        self.assertEqual(cursor.metrics.get("processed"), 1200)
        self.assertEqual(cursor.metrics.get("total_seen"), 1200)
        self.assertFalse(cursor.metrics.get("ingest_continuation_due"))
        self.assertTrue(cursor.metrics.get("ingest_complete_at"))
        self.assertTrue(cursor.metrics.get("capture_success_at"))

    def test_interrupt_then_continuation_without_refetch(self):
        spec = feed_by_id("AN_AMO10_XVII")
        raw = _actors_zip(1200)
        doc_id = _store_live_document(spec, raw, media_type="application/zip")
        digest = "b" * 64
        # Premier passage : 1 chunk seulement (budget max_chunks=1)
        first = normalize_and_ingest(
            feed_id="AN_AMO10_XVII",
            document_id=doc_id,
            parser_id="an_amo",
            execution_mode="live",
            chunk_size=500,
            drain=True,
            max_chunks=1,
        )
        self.assertFalse(first["snapshot_complete"])
        self.assertEqual(first["parse_cursor"]["processed"], 500)
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 500)

        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True,
            cadence="daily",
            last_success_at=timezone.now(),
            execution_mode=FeedCursor.ExecutionMode.LIVE,
            metrics={
                "ingest_continuation_due": True,
                "snapshot_complete": False,
                "parse_cursor": {**first["parse_cursor"], "snapshot_sha256": digest, "document_id": doc_id},
                "last_document_id": doc_id,
                "capture_success_at": timezone.now().isoformat(),
            },
        )
        collect_mock = mock.Mock(side_effect=AssertionError("re-fetch interdit en continuation"))
        with mock.patch("atelier.services.feeds.scheduler.collect_feed", collect_mock):
            # due malgré cadence daily pleine
            due = due_feeds(["daily"])
            self.assertTrue(any(c.feed_id == "AN_AMO10_XVII" for c in due))
            out = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
        collect_mock.assert_not_called()
        self.assertEqual(out.get("capture_status"), "skipped_continuation")
        # Continuer jusqu'à complet (plusieurs continue_ingest si budget)
        guard = 0
        while not FeedCursor.objects.get(feed_id="AN_AMO10_XVII").metrics.get("snapshot_complete"):
            guard += 1
            self.assertLess(guard, 10)
            with mock.patch("atelier.services.feeds.scheduler.collect_feed", collect_mock):
                run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 1200)
        collect_mock.assert_not_called()

    def test_new_snapshot_supersedes_incomplete_explicitly(self):
        spec = feed_by_id("AN_AMO10_XVII")
        doc_old = _store_live_document(spec, _actors_zip(100), media_type="application/zip")
        doc_new = _store_live_document(spec, _actors_zip(50), media_type="application/zip")
        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True,
            cadence="hourly",
            last_success_at=None,
            metrics={
                "snapshot_complete": False,
                "ingest_continuation_due": True,
                "capture_success_at": None,
                "parse_cursor": {
                    "snapshot_sha256": "c" * 64,
                    "document_id": doc_old,
                    "offset": 40,
                    "processed": 40,
                    "total_seen": 100,
                    "complete": False,
                },
            },
        )
        result = AdapterResult(
            status="created",
            feed_id="AN_AMO10_XVII",
            execution_mode="live",
            etag='W/"new"',
            cursor_token="tok-new",
            payload_digest="d" * 64,
            document_id=doc_new,
            parser_id="an_amo",
        )
        with mock.patch("atelier.services.feeds.scheduler.collect_feed", return_value=result):
            out = run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
        cursor = FeedCursor.objects.get(feed_id="AN_AMO10_XVII")
        transition = cursor.metrics.get("snapshot_transition") or {}
        self.assertEqual(transition.get("kind"), "superseded_incomplete")
        self.assertEqual(transition.get("previous_sha256"), "c" * 64)
        self.assertEqual(transition.get("new_sha256"), "d" * 64)
        self.assertTrue(out.get("snapshot_complete"))
        self.assertEqual(cursor.metrics.get("parse_cursor", {}).get("snapshot_sha256"), "d" * 64)
        # Pas de mélange : les 50 du nouveau snapshot sont présents
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:PA").count(), 50)


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R4FixtureOfflineStillWorks(AtelierTestCase):
    def test_offline_fixture_detail_via_url_map(self):
        ensure_feed_cursors()
        index_html = (
            b"<!DOCTYPE html><html><body>"
            b'<a href="https://www.elysee.fr/emmanuel-macron/2024/03/15/transparence">'
            b"Discours sur la transparence budgetaire</a></body></html>"
        )
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, index_html, media_type="text/html")
        result = normalize_and_ingest(
            feed_id="ELYSEE_DISCOURS",
            document_id=doc_id,
            parser_id="elysee_speech",
            execution_mode="fixture",
            drain=True,
        )
        self.assertEqual(result["details_fixture"], 1)
        self.assertEqual(result["declarations_created"], 1)
        self.assertEqual(Declaration.objects.count(), 1)
