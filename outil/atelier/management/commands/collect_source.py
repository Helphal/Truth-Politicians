from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from atelier.models import Source
from atelier.services.collector import CollectionError, fetch_source, inspect_zip


class Command(BaseCommand):
    help = "Collecte une source déjà qualifiée et activée, avec bornes de sécurité."

    def add_arguments(self, parser):
        parser.add_argument("source_id")
        parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(stable_id=options["source_id"])
        except Source.DoesNotExist as exc:
            raise CommandError("source_unknown") from exc
        try:
            result = fetch_source(source, max_bytes=options["max_bytes"])
            path = (settings.RUNTIME_DIR / result.document.original_path).resolve()
            archive_members = len(inspect_zip(path)) if path.suffix == ".zip" else None
        except CollectionError as exc:
            raise CommandError(f"collection_failed:{exc}") from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"source={source.stable_id} status={result.status} sha256={result.document.sha256} "
                f"bytes={result.document.byte_size} archive_members={archive_members}"
            )
        )
