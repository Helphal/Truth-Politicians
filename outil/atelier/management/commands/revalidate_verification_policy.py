from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError, PermissionDenied
from atelier.services.verification import revalidate_legacy_receipt


class Command(BaseCommand):
    help='Revalidation mécanique explicite 1.0 vers 1.1, sans nouvelle exécution sémantique ni revue humaine.'

    def add_arguments(self,parser):
        parser.add_argument('--receipt-id',type=int,required=True)
        parser.add_argument('--actor-id',type=int,required=True)

    def handle(self,*args,**options):
        try:
            user=get_user_model().objects.get(pk=options['actor_id'])
            result=revalidate_legacy_receipt(options['receipt_id'],user=user)
        except (ValidationError,PermissionDenied,get_user_model().DoesNotExist) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f'receipt={result.pk} policy={result.job.policy} decision={result.decision} mode=policy_only_revalidation')
