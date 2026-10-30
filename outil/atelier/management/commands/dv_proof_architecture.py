"""Local proof receipt. Never starts a provider or creates a personal review."""
import json
from django.core.management.base import BaseCommand, CommandError
from atelier.models import EditorialRevision
from atelier.services.proof_architecture import receive_local_assessment, assessment_for_revision

class Command(BaseCommand):
    help = 'Recompute/receive the four documentary layers; no semantic inference.'

    def add_arguments(self, parser):
        parser.add_argument('revision_id', type=int)
        parser.add_argument('--receive', action='store_true')

    def handle(self, *args, **options):
        try:
            revision = EditorialRevision.objects.select_related('dossier').get(pk=options['revision_id'])
        except EditorialRevision.DoesNotExist:
            raise CommandError('revision_missing') from None
        if options['receive']:
            receive_local_assessment(revision)
        report = assessment_for_revision(revision)
        if report is None:
            raise CommandError('proof_architecture_missing_or_stale')
        self.stdout.write(json.dumps({'schema': report['schema'], 'fingerprint': report['revision_fingerprint'],
            'assessment_digest': report['assessment_digest'], 'documentary_status': report['documentary_status'],
            'gates': report['gates'], 'counts': report['summary']['counts'], 'no_provider_call': True,
            'no_human_review_created': True}, ensure_ascii=False))
