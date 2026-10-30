"""Adaptateur ponctuel local. Aucun fournisseur/API ni ordonnanceur implicite."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from atelier.models import EditorialRevision, VerificationAttempt, VerificationJob, VerificationReceipt
from atelier.services import verification as service


class Command(BaseCommand):
    help = 'File DV-VERITE : status, enqueue, refresh, start, evidence, claims, finish, interrupt. Aucun envoi externe.'

    def add_arguments(self, parser):
        parser.add_argument('operation', choices=['status', 'enqueue', 'refresh', 'start', 'evidence', 'claims', 'finish', 'interrupt'])
        parser.add_argument('--id', type=int)
        parser.add_argument('--actor-id', type=int)
        parser.add_argument('--execution-id')
        parser.add_argument('--input', type=Path)
        parser.add_argument('--output', type=Path)

    def handle(self, *args, **options):
        operation = options['operation']
        try:
            user = get_user_model().objects.get(pk=options['actor_id']) if options['actor_id'] else None
            if operation == 'status':
                from django.db.models import Count
                from django.utils import timezone
                value = {'policy': service.POLICY,
                    'jobs': dict(VerificationJob.objects.values_list('state').annotate(count=Count('pk'))),
                    'receipts': dict(VerificationReceipt.objects.values_list('decision').annotate(count=Count('pk'))),
                    'expired_unfinished_attempts': VerificationAttempt.objects.filter(finished_at__isnull=True, expires_at__lte=timezone.now()).count(),
                    'provider': 'explicit_campaign_adapter_no_background_worker', 'publication': 'separate_server_gate_and_human_review'}
            elif operation == 'enqueue':
                revision = EditorialRevision.objects.get(pk=options['id'])
                revision.verification_required = True
                revision.save(update_fields=['verification_required'])
                job = service.enqueue(revision)
                value = {'job_id': job.pk, 'state': job.state, 'checks': service.deterministic(job)}
            elif operation == 'refresh':
                value = {'current_revisions_enqueued': service.refresh_pending()}
            elif operation == 'start':
                attempt = service.start(options['id'], user=user, execution_id=options['execution_id'] or '')
                value = service.evidence_packet(attempt)
            elif operation == 'claims':
                service.require_verifier(user)
                attempt = VerificationAttempt.objects.get(pk=options['id'], actor=user)
                value = service.claims_packet(attempt)
            elif operation == 'interrupt':
                service.interrupt(options['id'], user=user)
                value = {'state': 'interrupted'}
            else:
                path = options['input']
                if not path or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                    raise CommandError('input_absent_or_too_large')
                payload = json.loads(path.read_text(encoding='utf-8'))
                if operation == 'evidence':
                    attempt = service.record_evidence(options['id'], user=user, findings=payload)
                    value = service.claims_packet(attempt)
                else:
                    receipt = service.finish(options['id'], user=user, result=payload)
                    value = {'receipt_id': receipt.pk, 'decision': receipt.decision, 'fingerprint': receipt.fingerprint,
                             'human_review': 'not_granted'}
            if options['output']:
                path = options['output']
                if path.exists(): raise CommandError('output_exists')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
                self.stdout.write('Résultat enregistré ; aucun envoi externe.')
            else:
                self.stdout.write(json.dumps(value, ensure_ascii=False, indent=2))
        except (PermissionDenied, ValidationError, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
