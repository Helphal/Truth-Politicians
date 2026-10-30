import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from atelier.services.explain.qualified import export_qualified


class Command(BaseCommand):
    help = 'Exporte le dossier documentaire qualifié 0.2 avec preuves de consultation.'

    def add_arguments(self, parser):
        parser.add_argument('--qualification', required=True)
        parser.add_argument('--source-proofs', required=True)
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        try:
            dossier = json.loads(Path(options['qualification']).read_text(encoding='utf-8'))
            result = export_qualified(dossier, options['source_proofs'])
        except (ValueError, KeyError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        Path(options['output']).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(str(len(result['entries'])) + ' fiches documentaires qualifiées exportées')
