"""Ordonnanceur permanent multi-fréquences BUILD-002."""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from atelier.services.feeds.scheduler import cadence_seconds, run_cadence_once
from atelier.services.ingest import nocturnal_reconcile


class Command(BaseCommand):
    help = (
        "Exécute l'alimentation permanente (fast/hourly/daily) avec verrouillage, "
        "idempotence et réconciliation nocturne optionnelle."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--cadence",
            action="append",
            choices=["fast", "hourly", "daily", "nightly"],
            help="Répétable ; défaut = toutes sauf nightly sauf --reconcile",
        )
        parser.add_argument("--reconcile", action="store_true", help="Réconciliation nocturne FP-3")
        parser.add_argument(
            "--seed-only", action="store_true",
            help="Amorce les curseurs absents sous verrou, sans collecter ni réinitialiser les décisions existantes",
        )

    def handle(self, *args, **options):
        if options["seed_only"]:
            result = run_cadence_once([], seed_only=True)
            if result.get("skipped"):
                self.stdout.write(f"scheduler_skipped={result['skipped']}")
            else:
                self.stdout.write(f"feed_cursors_initialized created={result['feeds_created']}")
            return
        cadences = options["cadence"] or ["fast", "hourly", "daily"]
        if options["once"]:
            result = run_cadence_once(cadences)
            if result.get("skipped"):
                self.stdout.write(f"scheduler_skipped={result['skipped']}")
                return
            for row in result.get("results", []):
                self.stdout.write(
                    f"feed={row.get('feed_id')} status={row.get('status')} "
                    f"error={row.get('error_code', '')} objects={row.get('objects', 0)}"
                )
            if options["reconcile"] or "nightly" in cadences:
                recon = nocturnal_reconcile()
                self.stdout.write(f"nocturnal_reconcile={recon}")
            if result.get("success") is False:
                raise CommandError("permanent_scheduler_partial_failure")
            self.stdout.write("scheduler_run_complete=true")
            return
        while True:
            delay = min(cadence_seconds(c) for c in cadences if cadence_seconds(c) > 0) or 900
            self.stdout.write(f"scheduler_next_run_seconds={int(delay)} at={timezone.now().isoformat()}")
            time.sleep(max(1.0, float(delay)))
            result = run_cadence_once(cadences)
            if result.get("skipped"):
                self.stdout.write(f"scheduler_skipped={result['skipped']}")
                continue
            if options["reconcile"]:
                nocturnal_reconcile()
