"""Préparer un fichier privé signé ; aucun envoi ni activation de transport."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from atelier.models import EditorialRevision
from atelier.services.verification_export import build_site_packet, build_site_suspension


class Command(BaseCommand):
    help = 'Exporter une révision vérifiée et revue, sur fichier privé uniquement.'

    def add_arguments(self, parser):
        parser.add_argument('--operation', choices=['upsert', 'suspend'], default='upsert')
        parser.add_argument('--revision-id', type=int, required=True)
        parser.add_argument('--actor-id', type=int, required=True)
        parser.add_argument('--environment', choices=['test', 'private'], required=True)
        parser.add_argument('--sequence', type=int, required=True)
        parser.add_argument('--mapping', type=Path)
        parser.add_argument('--reason')
        parser.add_argument('--output', type=Path, required=True)

    def handle(self, *args, **options):
        try:
            path, output = options['mapping'], options['output']
            if output.exists() or output.is_symlink():
                raise CommandError('output_exists')
            revision = EditorialRevision.objects.get(pk=options['revision_id'])
            user = get_user_model().objects.get(pk=options['actor_id'])
            if options['operation'] == 'suspend':
                packet = build_site_suspension(revision, user=user, environment=options['environment'],
                                               sequence=options['sequence'], reason=options['reason'])
            else:
                if not path or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise CommandError('mapping_unsafe_or_too_large')
                packet = build_site_packet(revision, user=user, environment=options['environment'],
                    sequence=options['sequence'], mapping=json.loads(path.read_text(encoding='utf-8')))
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as handle:
                json.dump(packet, handle, ensure_ascii=False, indent=2)
            self.stdout.write('Fichier signé enregistré. Aucun transfert effectué.')
        except (ObjectDoesNotExist, PermissionDenied, ValidationError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
