import json
from django.core.management.base import BaseCommand
from atelier.services.private_preflight import assess_private_readiness

class Command(BaseCommand):
    help='Précontrôle privé sans valeur de secret, connexion ou modification.'
    def handle(self,*args,**options):
        self.stdout.write(json.dumps(assess_private_readiness(),ensure_ascii=False))
