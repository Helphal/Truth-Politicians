"""Requalification ciblée après modification d'une dépendance déjà liée."""
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import DocumentVersion, EvidencePassage, Source, EditorialRevision


def _schedule(source_id):
    def refresh():
        from .services.verification import enqueue
        # Les dossiers éditoriaux suivis, pas les millions de votes factuels.
        rows = EditorialRevision.objects.filter(verification_required=True).select_related('dossier')
        for revision in rows.iterator():
            if revision.revision != revision.dossier.current_revision:
                continue
            if any(isinstance(ref, dict) and ref.get('source_id') == source_id for ref in revision.source_snapshot):
                enqueue(revision)
    transaction.on_commit(refresh)


@receiver(post_save, sender=DocumentVersion)
def document_changed(sender, instance, raw=False, **kwargs):
    if not raw: _schedule(instance.source.stable_id)


@receiver(post_save, sender=Source)
def source_changed(sender, instance, raw=False, **kwargs):
    if not raw: _schedule(instance.stable_id)


@receiver(post_save, sender=EvidencePassage)
@receiver(post_delete, sender=EvidencePassage)
def passage_changed(sender, instance, raw=False, **kwargs):
    if not raw: _schedule(instance.document.source.stable_id)
