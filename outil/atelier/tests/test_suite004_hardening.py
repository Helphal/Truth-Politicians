from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from atelier.management.commands.run_private_scheduler import Command as Scheduler
from atelier.models import JobRun, Source
from atelier.services import analysis_provider
from atelier.services.collector import validate_source_url

from .support import AtelierTestCase


class Suite004BoundaryTests(SimpleTestCase):
    @override_settings(DV_SOURCE_ALLOWED_HOSTS={"www.senat.fr"})
    def test_protected_destination_check_does_not_resolve_dns(self):
        with patch("atelier.services.collector.resolve_public") as resolver:
            host, path, addresses = validate_source_url(
                "https://www.senat.fr/leg/test.html", resolve_dns=False
            )
        self.assertEqual((host, path, addresses), ("www.senat.fr", "/leg/test.html", ()))
        resolver.assert_not_called()

    def test_analysis_provider_proposal_rejects_wrong_types_and_unknown_act(self):
        candidate = {"included_acts": [{"scrutiny_id": "SENAT:178"}]}
        invalid = {
            "result": 123,
            "short_answer": {"wrong": "type"},
            "main_reservation": "Réserve.",
            "reasoning": "Raison.",
            "included_acts": [],
            "excluded_acts": [],
        }
        with self.assertRaises(analysis_provider.AnalysisProviderUnavailable):
            analysis_provider._proposal_from_content(invalid, candidate=candidate)
        invalid.update(result="CONFIRMED", short_answer="Réponse.")
        invalid["included_acts"] = [{"scrutiny_id": "SENAT:999"}]
        with self.assertRaisesMessage(analysis_provider.AnalysisProviderUnavailable, "analysis_provider_proposal_act_reference_invalid"):
            analysis_provider._proposal_from_content(invalid, candidate=candidate)

    def test_once_mode_distinguishes_failure_from_lock_skip(self):
        with patch.object(Scheduler, "_run_once", return_value=False):
            with self.assertRaisesMessage(CommandError, "scheduler_collection_failed"):
                Scheduler().handle(once=True)
        with patch.object(Scheduler, "_run_once", return_value=None):
            Scheduler().handle(once=True)


class Suite004SchedulerDurabilityTests(AtelierTestCase):
    def make_source(self):
        return Source.objects.create(
            stable_id="AN_AMO10_XVII",
            producer="Assemblée nationale",
            family="reference",
            discovery_url="https://data.assemblee-nationale.fr/fixture",
            document_url="https://data.assemblee-nationale.fr/fixture.zip",
            rights_status=Source.RightsStatus.OPEN,
            access_status=Source.AccessStatus.ACTIVE,
            operational_enabled=True,
        )

    def result(self, source):
        document = self.make_document(source=source, sha256=hashlib.sha256(b"new").hexdigest())
        return SimpleNamespace(document=document, created=True, status="downloaded")

    def test_failed_capture_retries_same_durable_job_then_caches(self):
        source = self.make_source()
        command = Scheduler()
        with patch("atelier.management.commands.run_private_scheduler.fetch_source", side_effect=RuntimeError("down")):
            self.assertFalse(command._collect_source(source.stable_id))
        job = JobRun.objects.get(task_name="private_source_collection")
        self.assertEqual((job.state, job.attempt), (JobRun.State.FAILED, 1))
        result = self.result(source)
        with patch("atelier.management.commands.run_private_scheduler.fetch_source", return_value=result), patch.object(
            command, "_post_capture", return_value={"pipeline": ["tested"]}
        ):
            self.assertTrue(command._collect_source(source.stable_id))
            self.assertTrue(command._collect_source(source.stable_id))
        job.refresh_from_db()
        self.assertEqual((job.state, job.attempt), (JobRun.State.SUCCEEDED, 2))
        self.assertEqual(JobRun.objects.filter(task_name="private_source_collection").count(), 1)

    def test_interrupted_capture_resumes_after_lease_without_duplicate(self):
        source = self.make_source()
        command = Scheduler()
        with patch("atelier.management.commands.run_private_scheduler.fetch_source", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                command._collect_source(source.stable_id)
        job = JobRun.objects.get(task_name="private_source_collection")
        self.assertEqual(job.state, JobRun.State.RUNNING)
        job.lease_until = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["lease_until", "updated_at"])
        result = self.result(source)
        with patch("atelier.management.commands.run_private_scheduler.fetch_source", return_value=result), patch.object(
            command, "_post_capture", return_value={"pipeline": ["resumed"]}
        ):
            self.assertTrue(command._collect_source(source.stable_id))
        job.refresh_from_db()
        self.assertEqual((job.state, job.attempt), (JobRun.State.SUCCEEDED, 2))
        self.assertEqual(JobRun.objects.filter(task_name="private_source_collection").count(), 1)
