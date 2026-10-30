"""GC dry-run / explicite des blobs heavy (STORAGE-01). Aucune purge NAS."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from atelier.services.storage_lifecycle import garbage_collect_heavy, storage_metrics


class Command(BaseCommand):
    help = "Garbage-collect des blobs heavy locaux ; dry-run par défaut."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="Purge réelle locale (pas NAS)")
        parser.add_argument("--metrics-only", action="store_true")

    def handle(self, *args, **options):
        if options["metrics_only"]:
            self.stdout.write(json.dumps(storage_metrics(), ensure_ascii=False, indent=2))
            return
        dry_run = not options["execute"]
        try:
            result = garbage_collect_heavy(dry_run=dry_run, explicit=options["execute"])
        except PermissionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "dry_run": result.dry_run,
                    "eligible": result.eligible,
                    "purged": result.purged,
                    "tombstones": result.tombstones,
                    "skipped_referenced": result.skipped_referenced,
                    "volume_bytes": result.volume_bytes,
                    "nas_touched": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
