"""BUILD-002-R1 — adaptateurs live, pipeline métier, matrice honnête, idempotence."""
from __future__ import annotations

from datetime import date

from django.core.management import call_command
from django.test import override_settings

from atelier.models import (
    Declaration,
    FeedCursor,
    LegislativeDossier,
    Measure,
    Person,
    RelationCandidate,
    StagedScrutiny,
    TextVersion,
)
from atelier.services.feeds.adapters import (
    LIVE_HTTP_ADAPTERS,
    collect_feed,
    credential_health,
    resolve_adapter,
    FixtureAdapter,
    HttpSnapshotAdapter,
    NotImplementedAdapter,
)
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import coverage_matrix, ensure_feed_cursors, run_cadence_once, run_feed
from atelier.tests.support import AtelierTestCase

@override_settings(DV_NETWORK_MODE="disabled")
class Build002R1OfflineTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        ensure_feed_cursors()
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

    def test_no_silent_fixture_when_network_direct(self):
        with override_settings(DV_NETWORK_MODE="direct"):
            spec = feed_by_id("AN_AMENDEMENTS_XVII")
            adapter = resolve_adapter(spec)
            self.assertIsInstance(adapter, HttpSnapshotAdapter)
            # Adaptateur inconnu → blocked_not_implemented, pas fixture
            from atelier.services.feeds.registry import FeedSpec

            fake = FeedSpec(
                feed_id="FAKE_X",
                authority="Test",
                family="x",
                cadence="daily",
                mechanism="x",
                discovery_url="https://www.assemblee-nationale.fr/",
                adapter="totally_unknown_adapter",
            )
            self.assertIsInstance(resolve_adapter(fake), NotImplementedAdapter)
            blocked = resolve_adapter(fake).collect(fake)
            self.assertEqual(blocked.error_code, "blocked_adapter_not_implemented")

    def test_live_adapters_cover_institutional_families(self):
        needed = {
            "an_amendements",
            "an_dossiers",
            "an_cr",
            "an_questions",
            "senat_acteurs",
            "senat_dosleg",
            "senat_ameli",
            "senat_cr",
            "senat_questions",
            "senat_scrutins",
            "elysee_speech",
            "gouv_speech",
        }
        self.assertTrue(needed.issubset(LIVE_HTTP_ADAPTERS))

    def test_pipeline_an_and_senat_produce_declarations_and_candidates(self):
        for feed_id in ("AN_COMPTES_RENDUS_XVII", "SENAT_COMPTES_RENDUS"):
            FeedCursor.objects.filter(feed_id=feed_id).update(
                ingestion_active=True, last_success_at=None, block_reason="", consecutive_errors=0
            )
        first = run_cadence_once(["hourly"], use_lock=False)
        statuses = {r["feed_id"]: r for r in first["results"] if r["feed_id"] in {"AN_COMPTES_RENDUS_XVII", "SENAT_COMPTES_RENDUS"}}
        self.assertIn("AN_COMPTES_RENDUS_XVII", statuses)
        self.assertIn("SENAT_COMPTES_RENDUS", statuses)
        self.assertEqual(statuses["AN_COMPTES_RENDUS_XVII"].get("capture_status"), "fixture")
        self.assertEqual(statuses["AN_COMPTES_RENDUS_XVII"].get("parse_status"), "ok")
        self.assertEqual(statuses["AN_COMPTES_RENDUS_XVII"].get("ingest_status"), "ok")
        self.assertGreaterEqual(Declaration.objects.count(), 2)
        self.assertGreaterEqual(RelationCandidate.objects.filter(status="proposed").count(), 1)
        an_cursor = FeedCursor.objects.get(feed_id="AN_COMPTES_RENDUS_XVII")
        self.assertEqual(an_cursor.execution_mode, FeedCursor.ExecutionMode.FIXTURE)
        self.assertIsNotNone(an_cursor.last_fixture_success_at)
        self.assertIsNone(an_cursor.last_live_success_at)

        # Idempotence : second run, pas de duplication Declaration
        before = Declaration.objects.count()
        before_rc = RelationCandidate.objects.count()
        # Forcer due en effaçant last_success
        FeedCursor.objects.filter(feed_id__in={"AN_COMPTES_RENDUS_XVII", "SENAT_COMPTES_RENDUS"}).update(
            last_success_at=None
        )
        run_cadence_once(["hourly"], use_lock=False)
        self.assertEqual(Declaration.objects.count(), before)
        self.assertEqual(RelationCandidate.objects.count(), before_rc)

    def test_an_votes_fixture_stages_scrutiny(self):
        FeedCursor.objects.filter(feed_id="AN_VOTES_XVII").update(
            ingestion_active=True, last_success_at=None, block_reason=""
        )
        cursor = FeedCursor.objects.get(feed_id="AN_VOTES_XVII")
        result = run_feed(cursor)
        self.assertEqual(result["parse_status"], "ok")
        self.assertGreaterEqual(StagedScrutiny.objects.count(), 1)

    def test_an_amo_creates_persons(self):
        FeedCursor.objects.filter(feed_id="AN_AMO10_XVII").update(
            ingestion_active=True, last_success_at=None, block_reason=""
        )
        run_feed(FeedCursor.objects.get(feed_id="AN_AMO10_XVII"))
        self.assertTrue(Person.objects.filter(stable_id="person:PA000001").exists())

    def test_parse_error_does_not_look_like_business_success(self):
        from atelier.services.feeds import pipeline as pipe

        original = pipe._dispatch_parser

        def boom(*args, **kwargs):
            raise ValueError("parse_boom")

        pipe._dispatch_parser = boom
        try:
            FeedCursor.objects.filter(feed_id="AN_VOTES_XVII").update(
                ingestion_active=True, last_success_at=None, etag="", cursor_token="", consecutive_errors=0
            )
            result = run_feed(FeedCursor.objects.get(feed_id="AN_VOTES_XVII"))
            self.assertEqual(result["capture_status"], "fixture")
            self.assertEqual(result["parse_status"], "error")
            cursor = FeedCursor.objects.get(feed_id="AN_VOTES_XVII")
            self.assertGreaterEqual(cursor.consecutive_errors, 1)
            self.assertEqual(cursor.last_error_code, "ValueError")
        finally:
            pipe._dispatch_parser = original

    def test_coverage_matrix_distinguishes_implementation(self):
        rows = {r["feed_id"]: r for r in coverage_matrix()}
        self.assertEqual(rows["LEGIFRANCE_JORF_PISTE"]["implementation"], "contract_only")
        self.assertEqual(rows["X_TWITTER"]["implementation"], "contract_only")
        self.assertEqual(rows["PRESS_RSS_GENERIC"]["implementation"], "config_blocked")
        self.assertEqual(rows["AN_VOTES_XVII"]["implementation"], "live_adapter")
        self.assertIn("last_live_success_at", rows["AN_VOTES_XVII"])
        self.assertIn("last_fixture_success_at", rows["AN_VOTES_XVII"])
        self.assertIn("capture_ok", rows["AN_VOTES_XVII"])
        self.assertIn("live_format_parser_implemented", rows["AN_VOTES_XVII"])
        self.assertTrue(rows["AN_VOTES_XVII"]["live_format_parser_implemented"])

    def test_legifrance_credential_health_ready(self):
        spec = feed_by_id("LEGIFRANCE_JORF_PISTE")
        health = credential_health(spec)
        self.assertFalse(health["secret_values_exposed"])
        self.assertIn("DV_LEGIFRANCE_CLIENT_ID", health["required_env_names"])
        self.assertFalse(health["credentials_present"])
        result = collect_feed("LEGIFRANCE_JORF_PISTE")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error_code, "contract_only")
        self.assertFalse(result.detail.get("ready_when_credentials_injected"))
        self.assertEqual(result.detail.get("implementation_status"), "adapter_not_implemented_after_credentials")

    def test_network_disabled_uses_fixture_only(self):
        spec = feed_by_id("AN_AMO10_XVII")
        self.assertIsInstance(resolve_adapter(spec), FixtureAdapter)

    def test_checks_and_seed(self):
        call_command("check")
        call_command("seed_build002")
