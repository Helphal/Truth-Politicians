from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from atelier.services.receiver import apply_packet


class Command(BaseCommand):
    help = "Applique un paquet au simulateur local ; ne contacte jamais WordPress."

    def add_arguments(self, parser):
        parser.add_argument("packet", help="Fichier JSON de paquet signé (maximum 5 Mio).")
        parser.add_argument("--resume-authorized", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["packet"]).resolve()
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            raise CommandError("packet_missing_or_too_large")
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError("packet_invalid_json") from exc
        result = apply_packet(packet, resume_authorized=options["resume_authorized"])
        self.stdout.write(json.dumps({"receiver": "local_simulator", "result": result}, ensure_ascii=False))
        if result not in {"applied_locally", "duplicate"}:
            raise CommandError(result)
