"""Export projection publique EXPLAIN pour le Site (G08)."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from atelier.services.explain import public_projection, seed_explain_entries


class Command(BaseCommand):
    help = "Exporte la projection publique EXPLAIN (JSON) pour import Site."

    def add_arguments(self, parser):
        parser.add_argument("--out", type=str, required=True, help="Chemin fichier JSON de sortie")
        parser.add_argument("--seed", action="store_true", help="Ensementer les fiches avant export")
        parser.add_argument(
            "--authorization-state",
            default="authorized",
            choices=["authorized", "draft", "withdrawn"],
        )

    def handle(self, *args, **options):
        if options["seed"]:
            result = seed_explain_entries()
            self.stdout.write(f"seed={result}")
        payload = public_projection(authorization_state=options["authorization_state"])
        out = Path(options["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(f"explain_public_export path={out} entries={len(payload['entries'])}")
