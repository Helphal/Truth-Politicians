from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from atelier.services.backup import BackupError, create_backup


class Command(BaseCommand):
    help = "Crée une sauvegarde privée vérifiée dans runtime/backups."

    def add_arguments(self, parser):
        parser.add_argument("--without-documents", action="store_true")

    def handle(self, *args, **options):
        stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        destination = settings.RUNTIME_DIR / "backups" / f"dv-atelier-{stamp}.zip"
        try:
            result = create_backup(destination, include_documents=not options["without_documents"])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"backup={destination.relative_to(settings.RUNTIME_DIR)} sha256={result['archive_sha256']} "
                f"bytes={result['archive_bytes']} files={len(result['files'])} "
                f"classification={result['classification']}"
            )
        )
