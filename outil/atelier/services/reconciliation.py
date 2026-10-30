"""Exposition distante conservatrice et clôture uniquement par reçu lié vérifié."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from atelier.models import AuditEvent, EditorialDossier, OutboxEvent, RemoteExposure


def remember_exposure(event):
    if event.operation == OutboxEvent.Operation.UPSERT and event.issued_at:
        return RemoteExposure.objects.get_or_create(event=event)[0]


def reconcile_exposures(environment):
    from .publication import evaluate_revision, queue_suspension
    # Rattrapage des bases anciennes et des émissions remplacées/failed : aucun
    # filtre sur l'état de transport. Les migrations font le même rattrapage.
    for event in OutboxEvent.objects.filter(environment=environment, operation="upsert", issued_at__isnull=False).iterator():
        remember_exposure(event)
    for exposure in RemoteExposure.objects.filter(event__environment=environment, resolved_at__isnull=True).select_related("event__snapshot__revision", "event__dossier"):
        event = exposure.event
        invalid = not event.snapshot_id or not evaluate_revision(event.snapshot.revision, environment).allowed
        if invalid or exposure.withdrawal_requested:
            queue_suspension(event.dossier, "Contenu retiré en attente de vérification de ses preuves et autorisations.", environment)


@transaction.atomic
def acknowledge_withdrawal(event, packet, receipt):
    from .receipts import verify_receipt
    if event.operation != "suspend" or str(event.event_id) != packet.get("event_id"):
        raise ValidationError("withdrawal_receipt_wrong_event")
    if not verify_receipt(receipt, packet):
        raise ValidationError("withdrawal_receipt_unverified")
    # Le paquet doit être EXACTEMENT celui généré par cette boîte d'envoi.
    from .publication import packet_for_event
    if packet_for_event(event, allow_delivered=True) != packet:
        raise ValidationError("withdrawal_packet_not_current")
    RemoteExposure.objects.filter(event__dossier_id=event.dossier_id,
        event__environment=event.environment, event__sequence__lt=event.sequence,
        resolved_at__isnull=True).update(withdrawal_requested=True, suspension=event,
            resolved_at=timezone.now(), verified_receipt=receipt)


@transaction.atomic
def retry_failed_withdrawal(event):
    """Reprise locale explicite, même identité/ordre ; jamais une boucle d'export."""
    event = OutboxEvent.objects.select_for_update().get(pk=event.pk)
    if event.operation != "suspend" or event.state not in {"failed", "delivered"}:
        raise ValidationError("not_failed_withdrawal")
    if not event.covered_exposures.filter(resolved_at__isnull=True).exists():
        raise ValidationError("no_unresolved_withdrawal")
    event.state, event.attempts = "pending", 0
    event.save(update_fields=["state", "attempts", "updated_at"])
    AuditEvent.objects.create(event_type="withdrawal_retry_requested", object_type="outbox_event",
        object_id=str(event.event_id), detail={"environment": event.environment})
    return event
