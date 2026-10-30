from pathlib import Path
from django.core.management.base import BaseCommand
from atelier.models import DocumentVersion
from atelier.services.archive_staging import stage_archive
import json

class Command(BaseCommand):
    help = 'Indexe un lot privé borné, identifié par document et empreinte ; aucun faux texte.'
    def add_arguments(self, parser):
        parser.add_argument('--document-id',required=True,type=int)
        parser.add_argument('--archive',required=True,type=Path)
        parser.add_argument('--batch-size',default=250,type=int)
    def handle(self,*args,**options):
        document = DocumentVersion.objects.select_related('source').get(pk=options['document_id'])
        self.stdout.write(json.dumps(stage_archive(document,options['archive'],batch_size=options['batch_size']),ensure_ascii=False))
