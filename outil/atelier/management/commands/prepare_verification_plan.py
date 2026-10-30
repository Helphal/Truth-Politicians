"""Freeze a current job without executing or allocating a provider phase."""
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from atelier.services.verification_executor import ExecutionFailure
from atelier.services.verification_protocol import Limits
from atelier.services.verification_plan_executor import prepare_plan


class Command(BaseCommand):
    help = 'Préparer le plan exact DV-VERITE et chiffrer ses phases, sans nouvel appel.'

    def add_arguments(self, parser):
        parser.add_argument('--job', type=int, required=True)
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--model', default='gpt-6-sol')
        parser.add_argument('--effort', default='high')
        parser.add_argument('--max-claims', type=int, default=32)
        parser.add_argument('--max-input-bytes', type=int, default=4 * 1024 * 1024)
        parser.add_argument('--max-output-bytes', type=int, default=128 * 1024)
        parser.add_argument('--resume-margin-phases', type=int, default=1)
        parser.add_argument('--reuse-source-plan-digest')
        parser.add_argument('--documentary-input',
            help='JSON explicite sous DV_VERIFICATION_DOCUMENTARY_ROOT ; originaux relus dans le coffre configuré.')

    def handle(self, **options):
        try:
            user = get_user_model().objects.defer('password').get(pk=options['user_id'])
            limits = Limits(max_claims=options['max_claims'], max_input_bytes=options['max_input_bytes'],
                max_output_bytes=options['max_output_bytes'], resume_margin_phases=options['resume_margin_phases'])
            path, plan = prepare_plan(options['job'], user=user, model=options['model'],
                effort=options['effort'], limits=limits, source_reuse_plan_digest=options['reuse_source_plan_digest'],
                documentary_input_path=options['documentary_input'])
            self.stdout.write(json.dumps({'plan_path': str(path), 'plan_digest': plan['plan_digest'],
                'allocation': plan['allocation_proposal'], 'provider_called': False,
                'publication': 'not_requested'}, ensure_ascii=False))
        except (ExecutionFailure, ValidationError, PermissionDenied) as error:
            code = error.code if isinstance(error, ExecutionFailure) else 'verification_plan_refused'
            raise CommandError(code) from None
