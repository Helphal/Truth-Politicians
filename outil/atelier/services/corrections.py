from __future__ import annotations

from django.db import transaction

from atelier.models import DocumentVersion, EditorialDossier


@transaction.atomic
def mark_analyses_affected(source_id: str, old_sha256: str, reason: str) -> int:
    """Suspend les analyses dont la preuve épinglée n'est plus courante."""
    affected = 0
    for dossier in EditorialDossier.objects.select_for_update().filter(
        content_kind=EditorialDossier.Kind.SPEECH_VOTE
    ):
        revision = dossier.revisions.filter(revision=dossier.current_revision).first()
        if not revision:
            continue
        if any(
            item.get("source_id") == source_id and item.get("sha256") == old_sha256
            for item in revision.source_snapshot if isinstance(item, dict)
        ):
            from .publication import queue_suspension
            from atelier.models import Environment
            environments = set(dossier.outbox_events.values_list("environment", flat=True)) or {Environment.TEST}
            for environment in environments:
                queue_suspension(dossier, "Une source a changé ; une nouvelle vérification est nécessaire.", environment)
            affected += 1
    return affected


def current_source_sha(source_id: str) -> str | None:
    document = DocumentVersion.objects.filter(source__stable_id=source_id, quarantined=False).order_by("-captured_at").first()
    return document.sha256 if document else None
