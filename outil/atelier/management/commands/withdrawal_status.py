import json
from django.core.management.base import BaseCommand
from atelier.models import RemoteExposure


class Command(BaseCommand):
    help = "État local des dettes de retrait, sans envoi ni clôture par simple statut de transport."

    def handle(self, *args, **options):
        debts = RemoteExposure.objects.filter(resolved_at__isnull=True)
        self.stdout.write(json.dumps({"unresolved_exposures":debts.count(),
            "withdrawal_requested":debts.filter(withdrawal_requested=True).count(),
            "failed_withdrawal_transport":debts.filter(withdrawal_requested=True, suspension__state="failed").count(),
            "resolved_with_verified_receipt":RemoteExposure.objects.filter(resolved_at__isnull=False).count()}))
