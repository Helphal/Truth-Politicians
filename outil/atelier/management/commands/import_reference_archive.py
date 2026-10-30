from pathlib import Path
from django.core.management.base import BaseCommand
from atelier.models import DocumentVersion
from atelier.services.reference_resolution import import_reference_archive
import json

class Command(BaseCommand):
    help='Import local borné d’une archive référentielle déjà qualifiée et épinglée.'
    def add_arguments(self, parser):
        parser.add_argument('--document-id',type=int,required=True)
        parser.add_argument('--archive',type=Path,required=True)
        parser.add_argument('--batch-size',type=int,default=1000)
    def handle(self,*args,**options):
        document=DocumentVersion.objects.select_related('source').get(pk=options['document_id'])
        self.stdout.write(json.dumps(import_reference_archive(document,options['archive'],batch_size=options['batch_size'])))
