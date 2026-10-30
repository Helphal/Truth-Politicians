"""Explicit bounded campaign; no daemon, scheduler or automatic retry."""
import json
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from atelier.services.verification_executor import CodexCLI, ExecutionFailure


class Command(BaseCommand):
    help = 'Entrée historique : diagnostic seul ; exécution via plan versionné et allocation explicite.'

    def add_arguments(self, parser):
        parser.add_argument('--preflight', action='store_true')
        parser.add_argument('--job', type=int)
        parser.add_argument('--user-id', type=int)
        parser.add_argument('--execution-id')
        parser.add_argument('--output', type=Path)
        parser.add_argument('--stop-file', type=Path)
        parser.add_argument('--resume-attempt', type=int)
        parser.add_argument('--timeout', type=int, default=180)
        parser.add_argument('--codex-executable', type=Path,
                            help='Chemin absolu du client dédié dont version et SHA sont qualifiés.')

    def handle(self, **options):
        # A historical START/command must not renew an exhausted allocation.
        # The versioned prepare/execute commands own new execution plans.
        if not options['preflight']:
            raise CommandError('legacy_execution_closed_use_versioned_plan_and_allocation')
        try:
            runner = CodexCLI(timeout=options['timeout'], executable=options['codex_executable'])
            self.stdout.write(json.dumps(runner.identity, sort_keys=True))
        except (ExecutionFailure, ValidationError, PermissionDenied) as error:
            code = error.code if isinstance(error, ExecutionFailure) else 'verification_service_refused'
            raise CommandError(code) from None
