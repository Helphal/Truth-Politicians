"""BUILD-002-R5 — file persistante discovery→détail, provenance evidence document."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from atelier.models import (
    Declaration,
    DetailWorkItem,
    DocumentVersion,
    FeedCursor,
    RelationCandidate,
)
from atelier.services.feeds.adapters import _store_live_document
from atelier.services.feeds.detail_fetch import DetailFetchResult
from atelier.services.feeds.detail_queue import (
    canonicalize_detail_url,
    detail_queue_counts,
    process_detail_queue,
)
from atelier.services.feeds.pipeline import (
    PROCESSOR,
    continue_detail_work_for_document,
    normalize_and_ingest,
)
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import (
    PROCESSOR_VERSION,
    continue_detail_work,
    due_feeds,
    ensure_feed_cursors,
    run_feed,
)
from atelier.tests.support import AtelierTestCase

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "build002" / "official_samples"

DETAIL_HTML_OK = (
    b"<!DOCTYPE html><html><body><h1>Discours test</h1>"
    b'<div data-author="Emmanuel Macron">'
    b'<p data-speech="1">Nous devons renforcer la transparence budgetaire nationale dans chaque debat public.</p>'
    b'<p data-speech="1">Cette exigence de clarte budgetaire engage la responsabilite de l executif devant la nation.</p>'
    b"</div></body></html>"
)


def _index_html(n: int, *, host: str = "www.elysee.fr") -> bytes:
    links = []
    for i in range(1, n + 1):
        links.append(
            f'<a href="https://{host}/emmanuel-macron/2024/03/{i:02d}/discours-{i}">'
            f"Discours numero {i} sur la transparence</a>"
        )
    body = "".join(links)
    return f"<!DOCTYPE html><html><body>{body}</body></html>".encode("utf-8")


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R5DetailQueueTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        ensure_feed_cursors()

    def test_25_discoveries_budget_10_persists_15_then_completes(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(25), media_type="text/html")
        parent = DocumentVersion.objects.get(pk=doc_id)
        stored_ids = {}

        def fake_resolve(url, **kwargs):
            if url not in stored_ids:
                stored_ids[url] = _store_live_document(
                    spec,
                    DETAIL_HTML_OK,
                    media_type="text/html",
                    provenance={"stage": "detail_fetch", "parent_document_id": parent.pk},
                )
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored_ids[url],
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            first = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
                max_details=10,
            )
        self.assertTrue(first["raw_snapshot_complete"])
        self.assertFalse(first["detail_work_complete"])
        self.assertEqual(DetailWorkItem.objects.filter(feed_id="ELYSEE_DISCOURS").count(), 25)
        pending = DetailWorkItem.objects.filter(
            feed_id="ELYSEE_DISCOURS", status=DetailWorkItem.Status.PENDING
        ).count()
        self.assertEqual(pending, 15)
        self.assertEqual(first["detail_pending"], 15)
        self.assertEqual(Declaration.objects.count(), 10)

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            second = continue_detail_work_for_document(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                max_details=20,
            )
        self.assertTrue(second["detail_work_complete"])
        self.assertEqual(Declaration.objects.count(), 25)
        counts = detail_queue_counts("ELYSEE_DISCOURS", parent_document_id=doc_id)
        self.assertTrue(counts["detail_work_complete"])
        self.assertEqual(counts["detail_pending"], 0)

    def test_retryable_then_success(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(1), media_type="text/html")
        parent = DocumentVersion.objects.get(pk=doc_id)
        calls = {"n": 0}

        def flaky_resolve(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return DetailFetchResult(status="failed", url=url, error_code="transient_http")
            stored = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=flaky_resolve):
            first = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
                max_details=5,
            )
        item = DetailWorkItem.objects.get(feed_id="ELYSEE_DISCOURS")
        self.assertEqual(item.status, DetailWorkItem.Status.RETRYABLE_ERROR)
        self.assertFalse(first["detail_work_complete"])
        self.assertEqual(Declaration.objects.count(), 0)

        # Échéance immédiate pour le retry
        DetailWorkItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=flaky_resolve):
            second = process_detail_queue(
                feed_id="ELYSEE_DISCOURS",
                parser_id="elysee_speech",
                execution_mode="live",
                parent_document=parent,
                max_details=5,
            )
            from atelier.services.feeds.pipeline import _ingest_detail_speech_items
            from atelier.models import Source

            source = Source.objects.get(stable_id="ELYSEE_DISCOURS")
            _ingest_detail_speech_items(
                second["speech_items"],
                source=source,
                document=parent,
                feed_id="ELYSEE_DISCOURS",
                parser_id="elysee_speech",
            )
        item.refresh_from_db()
        self.assertEqual(item.status, DetailWorkItem.Status.PARSED)
        self.assertEqual(Declaration.objects.count(), 1)

    def test_host_blocked_permanent(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        # Lien hors allowlist
        html = (
            b'<!DOCTYPE html><html><body>'
            b'<a href="https://evil.example/page">Discours pirate</a>'
            b"</body></html>"
        )
        doc_id = _store_live_document(spec, html, media_type="text/html")

        def blocked_resolve(url, **kwargs):
            return DetailFetchResult(status="blocked", url=url, error_code="host_not_allowed")

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=blocked_resolve):
            result = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        item = DetailWorkItem.objects.get(feed_id="ELYSEE_DISCOURS")
        self.assertEqual(item.status, DetailWorkItem.Status.BLOCKED_PERMANENT)
        self.assertTrue(result["detail_work_complete"])
        self.assertEqual(result["details_blocked"], 1)
        self.assertEqual(Declaration.objects.count(), 0)

    def test_no_duplicate_work_items_or_declarations(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(2), media_type="text/html")

        def fake_resolve(url, **kwargs):
            stored = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
            normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        self.assertEqual(DetailWorkItem.objects.filter(feed_id="ELYSEE_DISCOURS").count(), 2)
        self.assertEqual(Declaration.objects.count(), 2)

    def test_provenance_declaration_uses_detail_document(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(1), media_type="text/html")
        detail_ids = []

        def fake_resolve(url, **kwargs):
            stored = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")
            detail_ids.append(stored)
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        decl = Declaration.objects.get()
        self.assertEqual(decl.source_passage.document_id, detail_ids[0])
        self.assertNotEqual(decl.source_passage.document_id, doc_id)
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())

    def test_syceron_captured_document_as_evidence(self):
        spec = feed_by_id("AN_COMPTES_RENDUS_XVII")
        raw = (SAMPLES / "an_syceron_official_fragment.xml").read_bytes()
        doc_id = _store_live_document(spec, raw, media_type="application/xml")
        result = normalize_and_ingest(
            feed_id="AN_COMPTES_RENDUS_XVII",
            document_id=doc_id,
            parser_id="an_cr",
            execution_mode="live",
            drain=True,
        )
        self.assertGreaterEqual(result["declarations_created"], 1)
        decl = Declaration.objects.first()
        self.assertIsNotNone(decl)
        self.assertEqual(decl.source_passage.document_id, doc_id)
        receipt = DocumentVersion.objects.get(pk=doc_id).collection_receipt or {}
        self.assertNotEqual(receipt.get("execution_mode"), "synthetic_fallback")

    def test_no_auto_qualification(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(1), media_type="text/html")

        def fake_resolve(url, **kwargs):
            stored = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
            )
        self.assertEqual(Declaration.objects.count(), 1)
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())
        # Aucune auto-qualification : si un candidat existe, il reste proposed.
        for rc in RelationCandidate.objects.all():
            self.assertEqual(rc.status, "proposed")

    def test_scheduler_detail_continuation_without_refetch(self):
        spec = feed_by_id("ELYSEE_DISCOURS")
        doc_id = _store_live_document(spec, _index_html(3), media_type="text/html")

        def fake_resolve(url, **kwargs):
            stored = _store_live_document(spec, DETAIL_HTML_OK, media_type="text/html")
            return DetailFetchResult(
                status="fetched",
                url=url,
                document_id=stored,
                raw=DETAIL_HTML_OK,
                media_type="text/html",
            )

        with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
            first = normalize_and_ingest(
                feed_id="ELYSEE_DISCOURS",
                document_id=doc_id,
                parser_id="elysee_speech",
                execution_mode="live",
                drain=True,
                max_details=1,
            )
        self.assertTrue(first["raw_snapshot_complete"])
        self.assertFalse(first["detail_work_complete"])
        FeedCursor.objects.filter(feed_id="ELYSEE_DISCOURS").update(
            ingestion_active=True,
            cadence="daily",
            last_success_at=timezone.now(),
            execution_mode=FeedCursor.ExecutionMode.LIVE,
            metrics={
                "raw_snapshot_complete": True,
                "snapshot_complete": True,
                "detail_work_complete": False,
                "detail_continuation_due": True,
                "ingest_continuation_due": True,
                "detail_pending": first["detail_pending"],
                "detail_retryable": 0,
                "parse_cursor": {
                    **first["parse_cursor"],
                    "snapshot_sha256": "e" * 64,
                    "document_id": doc_id,
                    "complete": True,
                },
                "last_document_id": doc_id,
                "capture_success_at": timezone.now().isoformat(),
            },
        )
        collect_mock = mock.Mock(side_effect=AssertionError("re-fetch index interdit"))
        with mock.patch("atelier.services.feeds.scheduler.collect_feed", collect_mock):
            due = due_feeds(["daily"])
            self.assertTrue(any(c.feed_id == "ELYSEE_DISCOURS" for c in due))
            with mock.patch("atelier.services.feeds.detail_queue.resolve_detail", side_effect=fake_resolve):
                out = run_feed(FeedCursor.objects.get(feed_id="ELYSEE_DISCOURS"))
        collect_mock.assert_not_called()
        self.assertEqual(out.get("capture_status"), "skipped_detail_continuation")
        self.assertEqual(PROCESSOR, "dv-feed-pipeline/5")
        self.assertEqual(PROCESSOR_VERSION, "dv-permanent-scheduler/5")

    def test_canonicalize_strips_fragment(self):
        a = canonicalize_detail_url("https://WWW.Elysee.FR/path/?q=1#frag")
        b = canonicalize_detail_url("https://www.elysee.fr/path?q=1")
        self.assertEqual(a, b)
