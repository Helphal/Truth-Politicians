"""BUILD-002-R2 — parseurs formats officiels, provenance live, contract_only, E2E offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.core.management import call_command
from django.test import override_settings

from atelier.models import (
    Declaration,
    DocumentVersion,
    FeedCursor,
    Person,
    RelationCandidate,
    Source,
    StagedScrutiny,
)
from atelier.services.feeds.adapters import (
    ProviderUnavailableAdapter,
    collect_feed,
    resolve_adapter,
    _store_live_document,
)
from atelier.services.feeds.official_formats import (
    extract_items_from_bytes,
    extract_items_from_path,
    live_format_parser_implemented,
)
from atelier.services.feeds.pipeline import normalize_and_ingest
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import coverage_matrix, ensure_feed_cursors, run_feed
from atelier.tests.support import AtelierTestCase

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "build002" / "official_samples"


@override_settings(DV_NETWORK_MODE="disabled")
class Build002R2OfficialParseTests(AtelierTestCase):
    def test_an_votes_official_sample_parses(self):
        path = SAMPLES / "an_votes_official_one.json"
        result = extract_items_from_path(path, "an_votes")
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(result["item_count"], 1)
        item = result["items"][0]
        self.assertEqual(item["uid"], "VTANR5L17V2657")
        self.assertEqual(item["numero"], "2657")
        self.assertIn("official_normalized", item)

    def test_an_amo_official_zip_parses_person(self):
        path = SAMPLES / "an_amo_official_one.json.zip"
        result = extract_items_from_path(path, "an_amo", media_type="application/zip")
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["uid"].startswith("PA"))

    def test_an_amendements_questions_dossiers_samples(self):
        for name, parser in (
            ("an_amendements_official_one.json", "an_amendements"),
            ("an_questions_official_one.json", "an_questions"),
            ("an_dossiers_official_one.json", "an_dossiers"),
        ):
            result = extract_items_from_path(SAMPLES / name, parser)
            self.assertEqual(result["parse_status"], "ok", msg=name)
            self.assertGreaterEqual(result["item_count"], 1, msg=name)

    def test_an_syceron_fragment_yields_speech_items(self):
        result = extract_items_from_path(SAMPLES / "an_syceron_official_fragment.xml", "an_cr")
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(len(result["items"]), 1)
        quote = result["items"][0]["exact_quote"]
        self.assertGreater(len(quote), 40)
        self.assertNotIn("fixture", quote.casefold())

    def test_senat_odsen_and_scrutin_html(self):
        odsen = extract_items_from_path(SAMPLES / "senat_odsen_official_two.json", "senat_acteurs")
        self.assertEqual(odsen["parse_status"], "ok")
        self.assertEqual(len(odsen["items"]), 2)
        self.assertEqual(odsen["items"][0]["uid"], "21071F")

        scrutin = extract_items_from_path(
            SAMPLES / "senat_scrutin_detail_official.html",
            "senat_scrutins",
            source_url="https://www.senat.fr/scrutin-public/2025/scr2025-340.html",
        )
        self.assertEqual(scrutin["parse_status"], "ok")
        self.assertEqual(scrutin["items"][0]["numero"], "340")
        self.assertEqual(scrutin["items"][0]["syntheseVote"]["nombreVotants"], 345)

    def test_senat_akn_dosleg_elysee_samples(self):
        akn = extract_items_from_path(SAMPLES / "senat_akn_depots_head.xml", "senat_akn")
        self.assertGreaterEqual(akn["item_count"], 1)
        dosleg = extract_items_from_path(SAMPLES / "senat_dosleg_index_official.html", "senat_dosleg")
        self.assertGreaterEqual(dosleg["item_count"], 1)
        elysee = extract_items_from_path(SAMPLES / "elysee_index_official.html", "elysee_speech")
        self.assertGreaterEqual(elysee["item_count"], 1)

    def test_zip_no_longer_returns_empty_without_parser(self):
        raw = (SAMPLES / "an_votes_official_one.json.zip").read_bytes()
        result = extract_items_from_bytes(raw, "an_votes", media_type="application/zip")
        self.assertGreater(len(result["items"]), 0)

    def test_pipeline_official_an_votes_and_senat_odsen_ingest(self):
        ensure_feed_cursors()
        # AN votes via snapshot live-like
        an_spec = feed_by_id("AN_VOTES_XVII")
        an_raw = (SAMPLES / "an_votes_official_one.json.zip").read_bytes()
        an_doc_id = _store_live_document(an_spec, an_raw, media_type="application/zip")
        an_doc = DocumentVersion.objects.get(pk=an_doc_id)
        self.assertTrue(an_doc.parser_version.startswith("live:"))
        self.assertEqual(an_doc.collection_receipt.get("execution_mode"), "live")
        an_result = normalize_and_ingest(
            feed_id="AN_VOTES_XVII",
            document_id=an_doc_id,
            parser_id="an_votes",
            raw_items=None,
            execution_mode="live",
        )
        self.assertEqual(an_result["parse_status"], "ok")
        self.assertEqual(an_result["ingest_status"], "ok")
        self.assertEqual(an_result["payload_source"], "official_format")
        self.assertGreaterEqual(StagedScrutiny.objects.filter(source_uid="VTANR5L17V2657").count(), 1)

        # Sénat ODSEN
        se_spec = feed_by_id("SENAT_SENATEURS")
        se_raw = (SAMPLES / "senat_odsen_official_two.json").read_bytes()
        se_doc_id = _store_live_document(se_spec, se_raw, media_type="application/json")
        se_result = normalize_and_ingest(
            feed_id="SENAT_SENATEURS",
            document_id=se_doc_id,
            parser_id="senat_acteurs",
            raw_items=None,
            execution_mode="live",
        )
        self.assertEqual(se_result["parse_status"], "ok")
        self.assertTrue(Person.objects.filter(stable_id="person:21071F").exists())

    def test_institutional_speech_declaration_from_syceron_sample(self):
        ensure_feed_cursors()
        spec = feed_by_id("AN_COMPTES_RENDUS_XVII")
        raw = (SAMPLES / "an_syceron_official_fragment.xml").read_bytes()
        doc_id = _store_live_document(spec, raw, media_type="application/xml")
        before_rc = RelationCandidate.objects.count()
        result = normalize_and_ingest(
            feed_id="AN_COMPTES_RENDUS_XVII",
            document_id=doc_id,
            parser_id="an_cr",
            raw_items=None,
            execution_mode="live",
        )
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(result["declarations_created"], 1)
        decl = Declaration.objects.order_by("-id").first()
        self.assertIsNotNone(decl)
        self.assertTrue(decl.exact_quote)
        self.assertNotIn("fixture BUILD", decl.exact_quote)
        # Candidats non qualifiés
        self.assertGreaterEqual(RelationCandidate.objects.count(), before_rc)
        self.assertFalse(RelationCandidate.objects.filter(status="qualified").exists())

    def test_idempotent_official_ingest(self):
        ensure_feed_cursors()
        spec = feed_by_id("SENAT_SENATEURS")
        raw = (SAMPLES / "senat_odsen_official_two.json").read_bytes()
        doc_id = _store_live_document(spec, raw, media_type="application/json")
        first = normalize_and_ingest(
            feed_id="SENAT_SENATEURS", document_id=doc_id, parser_id="senat_acteurs", execution_mode="live"
        )
        before = Person.objects.filter(stable_id__startswith="person:").count()
        second = normalize_and_ingest(
            feed_id="SENAT_SENATEURS", document_id=doc_id, parser_id="senat_acteurs", execution_mode="live"
        )
        self.assertEqual(first["parse_status"], "ok")
        self.assertEqual(second["parse_status"], "ok")
        self.assertEqual(Person.objects.filter(stable_id__startswith="person:").count(), before)

    def test_live_media_snapshot_not_marked_fixture(self):
        ensure_feed_cursors()
        spec = feed_by_id("PRESS_RSS_GENERIC")
        raw = json.dumps({"items": [{"title": "x", "exact_quote": "citation presse test R2 " * 3}]}).encode()
        doc_id = _store_live_document(spec, raw, media_type="application/json")
        doc = DocumentVersion.objects.get(pk=doc_id)
        self.assertTrue(doc.parser_version.startswith("live:"))
        self.assertEqual(doc.collection_receipt.get("execution_mode"), "live")
        self.assertFalse(doc.parser_version.startswith("fixture:"))

    def test_credentials_are_contract_only_not_ready(self):
        result = collect_feed("LEGIFRANCE_JORF_PISTE")
        self.assertEqual(result.error_code, "contract_only")
        self.assertFalse(result.detail["ready_when_credentials_injected"])
        result_x = collect_feed("X_TWITTER")
        self.assertEqual(result_x.error_code, "contract_only")

    def test_gouv_provider_unavailable(self):
        with override_settings(DV_NETWORK_MODE="direct"):
            spec = feed_by_id("GOUV_DISCOURS")
            self.assertIsInstance(resolve_adapter(spec), ProviderUnavailableAdapter)
            result = resolve_adapter(spec).collect(spec)
            self.assertEqual(result.error_code, "provider_unavailable_or_waf")

    def test_coverage_matrix_r2_fields(self):
        ensure_feed_cursors()  # Amorçage explicite ; la couverture reste en lecture seule.
        rows = {r["feed_id"]: r for r in coverage_matrix()}
        an = rows["AN_VOTES_XVII"]
        self.assertTrue(an["live_format_parser_implemented"])
        self.assertEqual(an["live_checked_means"], "live_parse_and_ingest_verified_not_http_only")
        self.assertFalse(an["live_checked"])  # pas encore de preuve live parse+ingest en base
        self.assertEqual(rows["GOUV_DISCOURS"]["implementation"], "provider_unavailable")
        self.assertEqual(rows["LEGIFRANCE_JORF_PISTE"]["implementation"], "contract_only")

    def test_live_format_parser_flags(self):
        self.assertTrue(live_format_parser_implemented("an_votes"))
        self.assertTrue(live_format_parser_implemented("senat_acteurs"))
        self.assertFalse(live_format_parser_implemented("totally_unknown"))

    def test_fixture_scheduler_still_works_offline(self):
        ensure_feed_cursors()
        FeedCursor.objects.filter(feed_id="AN_VOTES_XVII").update(
            ingestion_active=True, last_success_at=None, block_reason="", consecutive_errors=0
        )
        result = run_feed(FeedCursor.objects.get(feed_id="AN_VOTES_XVII"))
        self.assertEqual(result["capture_status"], "fixture")
        self.assertEqual(result["parse_status"], "ok")

    def test_checks(self):
        call_command("check")
