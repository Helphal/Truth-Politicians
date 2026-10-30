"""Export borné de la boîte d'envoi, sans changer son état de livraison."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils import timezone

from atelier.models import Environment, OutboxEvent

from .canonical import canonical_bytes, sha256_digest
from .publication import packet_for_event, validate_event_current, invalidate_event, reissue_expired_event


EXPORT_FORMAT = "dv.outbox-batch/1"


def export_outbox(destination: Path, *, environment: str, limit: int = 100) -> dict:
    if environment not in Environment.values:
        raise ValidationError("unknown_environment")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValidationError("invalid_export_limit")
    destination = destination.resolve()
    if destination.exists():
        raise ValidationError("export_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)

    from .reconciliation import reconcile_exposures
    reconcile_exposures(environment)

    events = list(
        OutboxEvent.objects.filter(environment=environment, state__in=[OutboxEvent.State.PENDING, OutboxEvent.State.DELIVERED])
        .select_related("dossier")
        .order_by("operation", "created_at", "pk")
    )
    # Examiner toutes les copies en attente avant de borner le lot ; les suspensions
    # créées par invalidation ne doivent pas attendre le lot suivant.
    for event in events:
        try:
            validate_event_current(event, allow_delivered=True)
        except ValidationError as exc:
            reason = str(exc.messages[0])
            if reason == "event_expired_reissue_required":
                try:
                    reissue_expired_event(event)
                    continue
                except ValidationError as recheck:
                    reason = str(recheck.messages[0])
            invalidate_event(event, reason)
    events = list(OutboxEvent.objects.filter(environment=environment, state=OutboxEvent.State.PENDING).select_related("dossier").order_by("created_at", "pk"))
    manifest_items = []
    with tempfile.TemporaryDirectory(prefix="dv-export-", dir=destination.parent) as temp_name:
        temp = Path(temp_name)
        # Les suspensions passent avant tout upsert, y compris dans un lot de taille 1.
        events.sort(key=lambda event: event.operation != OutboxEvent.Operation.SUSPEND)
        for event in events:
            if len(manifest_items) >= limit:
                break
            try:
                packet = packet_for_event(event)
            except ValidationError as exc:
                invalidate_event(event, str(exc.messages[0]))
                continue
            filename = f"{event.sequence:08d}-{event.event_id}.json"
            content = canonical_bytes(packet)
            (temp / filename).write_bytes(content)
            manifest_items.append(
                {
                    "event_id": str(event.event_id),
                    "record_id": event.dossier.stable_id,
                    "record_revision": event.content_revision,
                    "sequence": event.sequence,
                    "operation": str(event.operation),
                    "file": filename,
                    "packet_sha256": sha256_digest(packet),
                    "bytes": len(content),
                }
            )
        manifest = {
            "format": EXPORT_FORMAT,
            "environment": environment,
            "generated_at": timezone.now().isoformat(),
            "bounded": True,
            "limit": limit,
            "count": len(manifest_items),
            "items": manifest_items,
        }
        (temp / "manifest.json").write_bytes(canonical_bytes(manifest))
        os.replace(temp, destination)
    return manifest
