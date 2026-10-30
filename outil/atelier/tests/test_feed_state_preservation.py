"""La lecture, l'amorçage et un verrou occupé ne réinitialisent pas les décisions."""
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from atelier.models import FeedCursor, JobRun, Source, UserProfile
from atelier.services.feeds.registry import feed_by_id
from atelier.services.feeds.scheduler import (
    coverage_matrix, due_feeds, ensure_feed_cursors, run_cadence_once, run_feed,
)
from atelier.services.file_lock import try_acquire_exclusive_nonblocking


@override_settings(DV_NETWORK_MODE="disabled", ALLOWED_HOSTS=["testserver"])
class FeedStatePreservationTests(TestCase):
    def setUp(self):
        self.spec = feed_by_id("AN_VOTES_XVII")
        self.registry = patch("atelier.services.feeds.scheduler.all_feed_specs", return_value=(self.spec,))
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def snapshot(self):
        return {
            "sources": list(Source.objects.order_by("pk").values()),
            "cursors": list(FeedCursor.objects.order_by("pk").values()),
            "jobs": list(JobRun.objects.order_by("pk").values()),
        }

    def assert_gets_are_read_only(self):
        user = get_user_model().objects.create_user(username="feed-operator-synth")
        UserProfile.objects.create(user=user, role=UserProfile.Role.OPERATOR,
                                   is_test_identity=True, must_change_password=False)
        self.client.force_login(user)
        before = self.snapshot()
        for path in ("/alimentation/", "/api/feeds/coverage/"):
            with self.subTest(path=path), CaptureQueriesContext(connection) as queries:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
            writes = [q["sql"] for q in queries if q["sql"].lstrip().split()[0].upper() in {
                "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP",
            } and any(table in q["sql"].lower() for table in (
                "atelier_source", "atelier_feedcursor", "atelier_jobrun",
            ))]
            self.assertEqual(writes, [])
            self.assertEqual(self.snapshot(), before)
        # L'activité de compte appartient au middleware ; la matrice elle-même
        # n'exécute que des lectures, y compris sur une base sans curseurs.
        with CaptureQueriesContext(connection) as queries:
            coverage_matrix()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))

    def test_empty_coverage_does_not_initialize_anything(self):
        self.assert_gets_are_read_only()
        self.assertEqual(self.client.get("/api/feeds/coverage/").json()["feeds"], [])

    def test_get_preserves_pause_rights_and_qualification_and_displays_persisted_rights(self):
        ensure_feed_cursors()
        Source.objects.update(rights_status=Source.RightsStatus.FORBIDDEN, qualified_at=None,
                              operational_enabled=False, qualification_notes="Qualification suspendue")
        FeedCursor.objects.update(ingestion_active=False, block_reason="rights_forbidden",
                                  execution_mode=FeedCursor.ExecutionMode.BLOCKED)
        self.assert_gets_are_read_only()
        row = self.client.get("/api/feeds/coverage/").json()["feeds"][0]
        self.assertEqual(row["rights"], "forbidden")
        self.assertFalse(row["ingestion_active"])

    def test_repeated_initialization_preserves_all_existing_fields(self):
        self.assertEqual(ensure_feed_cursors(), 1)
        Source.objects.update(rights_status=Source.RightsStatus.INTERNAL_ONLY,
                              access_status=Source.AccessStatus.RETIRED, operational_enabled=False,
                              qualification_notes="Décision persistée", document_url="https://example.org/exact")
        FeedCursor.objects.update(ingestion_active=False, cadence="manual", block_reason="rights_forbidden",
                                  cursor_token="position-a-conserver", metrics={"processed": 17})
        before = self.snapshot()
        self.assertEqual(ensure_feed_cursors(), 0)
        self.assertEqual(ensure_feed_cursors(activate_defaults=False), 0)
        self.assertEqual(self.snapshot(), before)

    def test_new_cursor_cannot_enable_existing_blocked_source(self):
        source = Source.objects.create(stable_id=self.spec.feed_id, producer="Source reçue", family="votes",
                                       discovery_url="https://example.org/source", rights_status="forbidden",
                                       access_status="blocked", operational_enabled=False)
        before = list(Source.objects.values())
        self.assertEqual(ensure_feed_cursors(), 1)
        self.assertFalse(FeedCursor.objects.get(source=source).ingestion_active)
        self.assertEqual(list(Source.objects.values()), before)

    def test_new_registry_restrictions_block_due_and_direct_execution_without_mutation(self):
        ensure_feed_cursors()
        before = self.snapshot()
        for change in ({"rights_status": "forbidden"}, {"block_reason": "provider_unavailable"}):
            with self.subTest(change=change), patch(
                "atelier.services.feeds.scheduler.all_feed_specs", return_value=(replace(self.spec, **change),)
            ), patch("atelier.services.feeds.scheduler.collect_feed") as collect:
                self.assertEqual(ensure_feed_cursors(), 0)
                self.assertEqual(due_feeds(["daily"]), [])
                result = run_feed(FeedCursor.objects.get(feed_id=self.spec.feed_id))
                self.assertEqual(result["status"], "blocked")
                collect.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_source_restriction_and_pause_block_direct_execution_without_mutation(self):
        ensure_feed_cursors()
        for change in ({"rights_status": "forbidden"}, {"access_status": "blocked"},
                       {"operational_enabled": False}):
            with self.subTest(change=change), patch("atelier.services.feeds.scheduler.collect_feed") as collect:
                Source.objects.update(**change)
                before = self.snapshot()
                self.assertEqual(due_feeds(["daily"]), [])
                self.assertEqual(run_feed(FeedCursor.objects.get())["status"], "blocked")
                collect.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                Source.objects.update(rights_status="open", access_status="qualified", operational_enabled=True)
        FeedCursor.objects.update(ingestion_active=False)
        before = self.snapshot()
        self.assertEqual(run_feed(FeedCursor.objects.get())["error_code"], "ingestion_paused")
        self.assertEqual(self.snapshot(), before)

    def test_occupied_lock_blocks_command_initialization_and_collection(self):
        with TemporaryDirectory() as directory, override_settings(RUNTIME_DIR=Path(directory)):
            lock_path = Path(directory) / "permanent-scheduler.lock"
            with lock_path.open("a+") as lock:
                self.assertTrue(try_acquire_exclusive_nonblocking(lock))
                before = self.snapshot()
                self.assertEqual(run_cadence_once(["daily"])["skipped"], "lock_held")
                for options in ({"seed_only": True}, {"once": True, "reconcile": True}):
                    with self.subTest(options=options), patch(
                        "atelier.management.commands.run_permanent_scheduler.nocturnal_reconcile"
                    ) as reconcile:
                        output = StringIO()
                        call_command("run_permanent_scheduler", stdout=output, **options)
                        self.assertIn("scheduler_skipped=lock_held", output.getvalue())
                        reconcile.assert_not_called()
                        self.assertEqual(self.snapshot(), before)
                with patch("atelier.management.commands.run_permanent_scheduler.time.sleep",
                           side_effect=[None, KeyboardInterrupt]), patch(
                    "atelier.management.commands.run_permanent_scheduler.nocturnal_reconcile"
                ) as reconcile:
                    with self.assertRaises(KeyboardInterrupt):
                        call_command("run_permanent_scheduler", reconcile=True, stdout=StringIO())
                    reconcile.assert_not_called()
                    self.assertEqual(self.snapshot(), before)

    def test_explicit_seed_initializes_only_absent_rows_under_lock_without_collecting(self):
        with TemporaryDirectory() as directory, override_settings(RUNTIME_DIR=Path(directory)), patch(
            "atelier.services.feeds.scheduler.run_feed"
        ) as collect:
            output = StringIO()
            call_command("run_permanent_scheduler", seed_only=True, stdout=output)
            self.assertIn("created=1", output.getvalue())
            self.assertEqual(Source.objects.count(), 1)
            self.assertEqual(FeedCursor.objects.count(), 1)
            FeedCursor.objects.update(ingestion_active=False)
            before = self.snapshot()
            call_command("run_permanent_scheduler", seed_only=True, stdout=StringIO())
            self.assertEqual(self.snapshot(), before)
            collect.assert_not_called()
