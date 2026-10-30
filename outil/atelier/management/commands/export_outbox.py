from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from atelier.services.exporter import export_outbox


class Command(BaseCommand):
    help = "Exporte un lot borné de paquets signés sans les marquer comme livrés."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Nouveau répertoire de destination.")
        parser.add_argument("--environment", choices=["test", "private", "production"], default="test")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        try:
            manifest = export_outbox(
                Path(options["output"]),
                environment=options["environment"],
                limit=options["limit"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({"status": "exported_locally", **manifest}, ensure_ascii=False))
