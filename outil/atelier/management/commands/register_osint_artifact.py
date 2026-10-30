"""Raccorde à l'atelier un original déjà capturé et vérifié par le service de collecte protégé."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from atelier.models import DocumentVersion, Source


class Command(BaseCommand):
    help = "Enregistre une capture locale du service de collecte protégé après vérification d'empreinte, sans accès réseau."

    def add_arguments(self, parser):
        parser.add_argument("--source-id", required=True)
        parser.add_argument("--artifact", type=Path, required=True)
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--job-id", required=True)
        parser.add_argument("--original-artifact-id", required=True)
        parser.add_argument("--warc-artifact-id", required=True)
        parser.add_argument("--media-type", required=True)

    def handle(self, *args, **options):
        artifact = options["artifact"].resolve(strict=True)
        expected = options["expected_sha256"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CommandError("sha256_invalid")
        for field in ("job_id", "original_artifact_id", "warc_artifact_id"):
            if not re.fullmatch(r"[0-9a-f-]{36}", options[field]):
                raise CommandError(f"{field}_invalid")
        digest = hashlib.sha256()
        size = 0
        with artifact.open("rb") as source_file:
            while block := source_file.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != expected:
            raise CommandError("artifact_digest_mismatch")
        try:
            source = Source.objects.get(stable_id=options["source_id"], operational_enabled=True)
        except Source.DoesNotExist as exc:
            raise CommandError("source_unknown_or_disabled") from exc

        storage = (settings.RUNTIME_DIR / "raw-snapshots").resolve()
        storage.mkdir(parents=True, exist_ok=True)
        suffix = artifact.suffix if artifact.suffix in {".zip", ".xml", ".raw"} else ".bin"
        safe_source_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in source.stable_id)
        target = (storage / f"{safe_source_id}-{expected}{suffix}").resolve()
        if storage not in target.parents:
            raise CommandError("storage_escape")
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=storage, prefix="dv-osint-", suffix=".part", delete=False) as temp:
                temp_path = Path(temp.name)
                with artifact.open("rb") as source_file:
                    shutil.copyfileobj(source_file, temp)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_path, target)
        receipt = {
            "transport": "protected_capture",
            "egress_profile": "TOR",
            "job_id": options["job_id"],
            "job_status": "SUCCEEDED",
            "requested_url": source.document_url,
            "final_url": source.document_url,
            "status_code": 200,
            "content_sha256": expected,
            "size_bytes": size,
            "original_artifact_id": options["original_artifact_id"],
            "warc_artifact_id": options["warc_artifact_id"],
            "complete": True,
            "registered_from_verified_local_artifact": True,
            "no_direct_fallback": True,
        }
        with transaction.atomic():
            document, created = DocumentVersion.objects.get_or_create(
                source=source,
                sha256=expected,
                defaults={
                    "version_id": expected[:20],
                    "captured_at": timezone.now(),
                    "media_type": options["media_type"],
                    "byte_size": size,
                    "original_path": str(target.relative_to(settings.RUNTIME_DIR)),
                    "public_copy_allowed": source.rights_status == Source.RightsStatus.OPEN,
                    "collection_receipt": receipt,
                },
            )
            if not created and not document.collection_receipt:
                document.collection_receipt = receipt
                document.save(update_fields=["collection_receipt", "updated_at"])
            source.access_status = Source.AccessStatus.ACTIVE
            source.last_success_at = timezone.now()
            source.last_failure_code = ""
            source.save(update_fields=["access_status", "last_success_at", "last_failure_code", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"source={source.stable_id} document_id={document.pk} created={created} sha256={expected} bytes={size}"
            )
        )
