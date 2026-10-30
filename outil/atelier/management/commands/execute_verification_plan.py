"""A real phase needs an explicit unconsumed allocation before CLI preflight."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from atelier.services.verification_executor import ExecutionFailure
from atelier.services.verification_plan_executor import execute_plan


class Command(BaseCommand):
    help = 'Exécuter/reprendre un plan DV-VERITE figé sous allocation explicite, sans publication.'

    def add_arguments(self, parser):
        parser.add_argument('--plan', type=Path, required=True)
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--execution-id', required=True)
        parser.add_argument('--allocation', type=Path, required=True)
        parser.add_argument('--retry-batch', action='append', default=[])
        parser.add_argument('--stop-file', type=Path)
        parser.add_argument('--timeout', type=int, default=600)
        parser.add_argument('--codex-executable', type=Path)
        parser.add_argument('--pilot-batches', type=int,
            help='Pause après ce nombre de sous-lots acceptés, avant toute phase suivante ; absent par défaut.')
        parser.add_argument('--transport', choices=('legacy', 'lossless-v1'), default='legacy',
            help='Transport explicite sans perte ; le plan logique et la politique restent identiques.')
        parser.add_argument('--close-lease-at-checkpoint', action='store_true',
            help='Fermer le bail possédé à la frontière reçue pour une reprise normale sans régénération.')

        parser.add_argument('--pause-before-launch-file', type=Path)

    def handle(self, **options):
        try:
            user = get_user_model().objects.defer('password').get(pk=options['user_id'])
            receipt = execute_plan(options['plan'], user=user, execution_id=options['execution_id'],
                allocation_path=options['allocation'], retry_batches=options['retry_batch'],
                stop_file=options['stop_file'], timeout=options['timeout'], codex_executable=options['codex_executable'],
                stop_after_accepted_batches=options['pilot_batches'], transport=options.get('transport', 'legacy'),
                close_lease_at_checkpoint=options.get('close_lease_at_checkpoint', False),
                pause_before_launch_file=options.get('pause_before_launch_file'))
            self.stdout.write(json.dumps({'receipt_id': receipt.pk, 'decision': receipt.decision,
                'publication': 'not_requested', 'automatic_retry': False}))
        except (ExecutionFailure, ValidationError, PermissionDenied) as error:
            code = error.code if isinstance(error, ExecutionFailure) else 'verification_plan_refused'
            if code == 'pilot_checkpoint_received':
                self.stdout.write(json.dumps({'status': code, 'receipt_id': None,
                    'publication': 'not_requested', 'automatic_retry': False}))
                return
            raise CommandError(code) from None
