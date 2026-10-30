from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from atelier.services.backup import BackupError, restore_backup


class Command(BaseCommand):
    help = "Restaure une sauvegarde dans une zone isolée vide ; ne remplace jamais la base active."

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--destination", required=True)

    def handle(self, *args, **options):
        archive = Path(options["archive"]).resolve()
        destination = Path(options["destination"]).resolve()
        runtime = settings.RUNTIME_DIR.resolve()
        if runtime not in destination.parents or destination == runtime:
            raise CommandError("restore_destination_must_be_isolated_under_runtime")
        try:
            result = restore_backup(archive, destination)
        except (BackupError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"status={result['status']} files={result['files']} tables={result['tables']} "
                f"destination={destination.relative_to(runtime)} active_database_untouched=true"
            )
        )
