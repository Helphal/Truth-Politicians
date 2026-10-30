from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from atelier.models import OutboxEvent
from atelier.services.reconciliation import retry_failed_withdrawal


class Command(BaseCommand):
    help = "Réarme explicitement une suspension non réconciliée : même événement, trois essais, aucun envoi."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", required=True)

    def handle(self, *args, **options):
        try:
            event = OutboxEvent.objects.get(event_id=options["event_id"])
            retry_failed_withdrawal(event)
        except (OutboxEvent.DoesNotExist, ValidationError) as exc:
            raise CommandError("withdrawal_retry_refused") from exc
        self.stdout.write("withdrawal_rearmed same_identity=true network_calls=0 receipt_still_required=true")
