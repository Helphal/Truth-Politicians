"""Ordonnanceur privé minimal pour les sources dynamiques qualifiées."""
from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from atelier.models import AuditEvent, JobRun, Source
from atelier.services.canonical import sha256_digest
from atelier.services.collector import fetch_source, inspect_zip
from atelier.services.file_lock import try_acquire_exclusive_nonblocking
from atelier.services.jobs import claim_job, finish_job
from atelier.services.reference_resolution import import_reference_archive


SOURCES = ("AN_AMO10_XVII", "SENAT_AKN_DEPOTS")
PROCESSOR_VERSION = "dv-private-scheduler/2"
MAX_ATTEMPTS_PER_CAPTURE = 3


def next_delay_seconds(now, *, hour: int = 3, minute: int = 17) -> float:
    local = timezone.localtime(now)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return max(1.0, (target - local).total_seconds())


class Command(BaseCommand):
    help = "Exécute quotidiennement les collectes dynamiques, sans chevauchement ni repli direct."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def _post_capture(self, source: Source, result) -> dict:
        path = (settings.RUNTIME_DIR / result.document.original_path).resolve()
        if settings.RUNTIME_DIR.resolve() not in path.parents or not path.is_file():
            raise RuntimeError("scheduler_captured_original_missing")
        details = {
            "capture_status": result.status,
            "document_id": result.document.pk,
            "document_sha256": result.document.sha256,
            "new_document": result.created,
            "pipeline": ["structure_validated"],
        }
        if path.suffix == ".zip":
            details["archive_members"] = len(inspect_zip(path))
        if source.stable_id == "AN_AMO10_XVII" and result.created:
            details["reference_import"] = import_reference_archive(result.document, path, batch_size=1_000)
            details["pipeline"].append("reference_import_resumed")
        elif source.stable_id == "AN_AMO10_XVII":
            details["pipeline"].append("reference_import_unchanged_not_restarted")
        else:
            details["pipeline"].append("source_specific_import_manual")
        AuditEvent.objects.create(
            actor=None,
            event_type="private_collection_pipeline",
            object_type="DocumentVersion",
            object_id=str(result.document.pk),
            detail={**details, "publication_authorized": False, "analysis_provider_required": False},
        )
        details["pipeline"].append("affected_items_recorded")
        return details

    def _collect_source(self, source_id: str) -> bool:
        source = Source.objects.get(stable_id=source_id)
        capture_key = timezone.localdate().isoformat()
        input_digest = sha256_digest(
            {
                "source_id": source.stable_id,
                "document_url": source.document_url,
                "discovery_url": source.discovery_url,
                "capture_key": capture_key,
            }
        )
        job, _ = JobRun.objects.get_or_create(
            task_name="private_source_collection",
            batch_id=f"{capture_key}:{source_id}",
            input_digest=input_digest,
            processor_version=PROCESSOR_VERSION,
            defaults={
                "configuration": {
                    "source_id": source_id,
                    "capture_key": capture_key,
                    "max_attempts": MAX_ATTEMPTS_PER_CAPTURE,
                    "no_direct_fallback": True,
                }
            },
        )
        if job.state == JobRun.State.SUCCEEDED:
            self.stdout.write(f"scheduler_source_cached={source_id} job={job.job_id}")
            return True
        if job.attempt >= MAX_ATTEMPTS_PER_CAPTURE and job.state != JobRun.State.RUNNING:
            self.stderr.write(f"scheduler_source_terminal={source_id} attempts={job.attempt}")
            return False
        claimed = claim_job(job.job_id, lease_seconds=900)
        if claimed is None:
            self.stderr.write(f"scheduler_source_busy={source_id} job={job.job_id}")
            return False
        try:
            result = fetch_source(source, max_bytes=10 * 1024 * 1024, max_seconds=180)
            details = self._post_capture(source, result)
        except Exception as exc:
            finish_job(job.job_id, succeeded=False, error_code=type(exc).__name__)
            self.stderr.write(f"scheduler_source_failed={source_id} error={type(exc).__name__}")
            return False
        with transaction.atomic():
            current = JobRun.objects.select_for_update().get(job_id=job.job_id)
            current.configuration = {**current.configuration, "result": details}
            current.save(update_fields=["configuration", "updated_at"])
        finish_job(job.job_id, succeeded=True)
        self.stdout.write(
            f"scheduler_source_complete={source_id} status={result.status} document={result.document.pk}"
        )
        return True

    def _run_once(self) -> bool | None:
        lock_path = settings.RUNTIME_DIR / "private-scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            if not try_acquire_exclusive_nonblocking(lock):
                self.stderr.write("scheduler_skipped=lock_unavailable_or_held")
                return None
            success = True
            for source_id in SOURCES:
                try:
                    if not self._collect_source(source_id):
                        success = False
                except Exception as exc:
                    success = False
                    self.stderr.write(f"scheduler_source_failed={source_id} error={type(exc).__name__}")
            self.stdout.write(f"scheduler_run_complete={str(success).lower()}")
            return success

    def handle(self, *args, **options):
        if options["once"]:
            result = self._run_once()
            if result is False:
                from django.core.management.base import CommandError

                raise CommandError("scheduler_collection_failed")
            return
        while True:
            delay = next_delay_seconds(timezone.now())
            self.stdout.write(f"scheduler_next_run_seconds={int(delay)}")
            time.sleep(delay)
            self._run_once()
