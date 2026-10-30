"""STORAGE-01 — cycle de vie intelligent des archives (métadonnées vs blobs)."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from atelier.models import AuditEvent, BlobReference, BlobTombstone, StoredBlob


@dataclass(frozen=True)
class RetentionThresholds:
    light_max_bytes: int
    medium_max_bytes: int

    @classmethod
    def from_settings(cls) -> "RetentionThresholds":
        return cls(
            light_max_bytes=int(getattr(settings, "DV_BLOB_LIGHT_MAX_BYTES", 256 * 1024)),
            medium_max_bytes=int(getattr(settings, "DV_BLOB_MEDIUM_MAX_BYTES", 5 * 1024 * 1024)),
        )


def classify_retention(byte_size: int, media_type: str = "") -> str:
    thresholds = RetentionThresholds.from_settings()
    heavy_types = {
        "video/",
        "audio/",
        "application/warc",
        "application/wacz",
    }
    lowered = (media_type or "").lower()
    if any(lowered.startswith(prefix) for prefix in heavy_types) or byte_size > thresholds.medium_max_bytes:
        return StoredBlob.RetentionClass.HEAVY
    if byte_size <= thresholds.light_max_bytes and (
        lowered.startswith("text/")
        or lowered in {"application/json", "application/xml", "text/xml", ""}
        or lowered.endswith("+json")
        or lowered.endswith("+xml")
    ):
        return StoredBlob.RetentionClass.LIGHT
    if byte_size <= thresholds.medium_max_bytes:
        return StoredBlob.RetentionClass.MEDIUM
    return StoredBlob.RetentionClass.HEAVY


def _blob_root() -> Path:
    root = Path(settings.RUNTIME_DIR).resolve() / "blob-store"
    root.mkdir(parents=True, exist_ok=True)
    return root


@transaction.atomic
def register_blob(
    *,
    content: bytes,
    media_type: str,
    source_url: str = "",
    rights_note: str = "",
    metadata: dict | None = None,
    write_bytes: bool = True,
) -> StoredBlob:
    digest = hashlib.sha256(content).hexdigest()
    existing = StoredBlob.objects.filter(content_hash=digest).first()
    if existing:
        return existing
    retention = classify_retention(len(content), media_type)
    path = ""
    if write_bytes and retention != StoredBlob.RetentionClass.HEAVY:
        # Heavy : chemin optionnel ; politique = ne pas stocker indéfiniment.
        target = _blob_root() / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        path = str(target.relative_to(Path(settings.RUNTIME_DIR).resolve()))
    elif write_bytes and retention == StoredBlob.RetentionClass.HEAVY:
        target = _blob_root() / "heavy" / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        path = str(target.relative_to(Path(settings.RUNTIME_DIR).resolve()))
    return StoredBlob.objects.create(
        content_hash=digest,
        byte_size=len(content),
        media_type=media_type or "application/octet-stream",
        retention_class=retention,
        storage_path=path,
        source_url=source_url,
        rights_note=rights_note,
        metadata=metadata or {},
    )


def attach_blob(
    blob: StoredBlob,
    *,
    object_type: str,
    object_id: str,
    role: str = "evidence",
    conserving_status: str = BlobReference.Conserving.ACTIVE,
) -> BlobReference:
    ref, _ = BlobReference.objects.get_or_create(
        blob=blob,
        object_type=object_type,
        object_id=object_id,
        role=role,
        defaults={"conserving_status": conserving_status},
    )
    return ref


def mark_dossier_rejected(object_type: str, object_id: str, *, reason: str) -> int:
    now = timezone.now()
    updated = BlobReference.objects.filter(
        object_type=object_type,
        object_id=object_id,
        conserving_status=BlobReference.Conserving.ACTIVE,
    ).update(
        conserving_status=BlobReference.Conserving.REJECTED,
        rejection_reason=reason,
        rejected_at=now,
    )
    _refresh_purge_eligibility()
    return updated


def reactivate_reference(object_type: str, object_id: str) -> int:
    updated = BlobReference.objects.filter(
        object_type=object_type,
        object_id=object_id,
        conserving_status=BlobReference.Conserving.REJECTED,
    ).update(
        conserving_status=BlobReference.Conserving.ACTIVE,
        rejection_reason="",
        rejected_at=None,
    )
    StoredBlob.objects.filter(
        lifecycle=StoredBlob.Lifecycle.PURGE_ELIGIBLE,
        references__object_type=object_type,
        references__object_id=object_id,
    ).update(lifecycle=StoredBlob.Lifecycle.ACTIVE)
    return updated


def _rejection_anchor(reference: BlobReference):
    """Ancre de quarantaine : rejected_at métier, jamais created_at/updated_at seuls.

    Historiques REJECTED sans rejected_at : ancre inconnue → rester en quarantaine
    (pas d'éligibilité inventée).
    """
    return reference.rejected_at


def _blob_still_purge_eligible(blob: StoredBlob) -> bool:
    if blob.references.filter(
        conserving_status__in={
            BlobReference.Conserving.ACTIVE,
            BlobReference.Conserving.ARCHIVAL,
        }
    ).exists():
        return False
    rejected_links = list(
        blob.references.filter(conserving_status=BlobReference.Conserving.REJECTED)
    )
    if not rejected_links:
        return False
    anchors = [_rejection_anchor(r) for r in rejected_links]
    if any(a is None for a in anchors):
        return False
    quarantine_days = int(getattr(settings, "DV_BLOB_QUARANTINE_DAYS", 7))
    cutoff = timezone.now() - timedelta(days=quarantine_days)
    return max(anchors) <= cutoff


def _unlink_blob_file(path: Path) -> None:
    """Point unique de suppression fichier (réévaluable en banc CR03)."""
    os.unlink(path)


def _refresh_purge_eligibility() -> int:
    """Heavy éligible seulement si plus aucun lien conservateur actif/archival."""
    marked = 0
    quarantine_days = int(getattr(settings, "DV_BLOB_QUARANTINE_DAYS", 7))
    cutoff = timezone.now() - timedelta(days=quarantine_days)
    for blob in StoredBlob.objects.filter(retention_class=StoredBlob.RetentionClass.HEAVY).exclude(
        lifecycle=StoredBlob.Lifecycle.TOMBSTONED
    ):
        conserving = blob.references.filter(
            conserving_status__in={
                BlobReference.Conserving.ACTIVE,
                BlobReference.Conserving.ARCHIVAL,
            }
        ).exists()
        if conserving:
            if blob.lifecycle != StoredBlob.Lifecycle.ACTIVE:
                blob.lifecycle = StoredBlob.Lifecycle.ACTIVE
                blob.save(update_fields=["lifecycle", "updated_at"])
            continue
        rejected_links = list(
            blob.references.filter(conserving_status=BlobReference.Conserving.REJECTED)
        )
        if not rejected_links:
            continue
        # Une date de rejet inconnue parmi les rejets pertinents bloque l'éligibilité
        # (même si d'autres rejets ont une date ancienne connue).
        anchors = [_rejection_anchor(r) for r in rejected_links]
        if any(a is None for a in anchors):
            blob.lifecycle = StoredBlob.Lifecycle.QUARANTINE
            blob.save(update_fields=["lifecycle", "updated_at"])
            continue
        latest_reject_at = max(anchors)
        if latest_reject_at > cutoff:
            blob.lifecycle = StoredBlob.Lifecycle.QUARANTINE
            blob.save(update_fields=["lifecycle", "updated_at"])
            continue
        blob.lifecycle = StoredBlob.Lifecycle.PURGE_ELIGIBLE
        blob.save(update_fields=["lifecycle", "updated_at"])
        marked += 1
    return marked


@dataclass
class GCResult:
    dry_run: bool
    eligible: int
    purged: int
    tombstones: list[str]
    skipped_referenced: int
    volume_bytes: int


def garbage_collect_heavy(*, dry_run: bool = True, explicit: bool = False) -> GCResult:
    """
    GC des blobs heavy. Jamais silencieux : audit + dry-run par défaut.
    Ne purge aucun contenu NAS réel ; opère sur le blob-store runtime local.
    """

    def safe_path(storage_path: str) -> Path | None:
        if not storage_path or not str(storage_path).strip():
            return None
        raw = Path(storage_path)
        if raw.is_absolute() or ".." in raw.parts:
            return None
        runtime = Path(settings.RUNTIME_DIR).resolve()
        blob_root = (runtime / "blob-store").resolve()
        candidate = (runtime / raw).resolve()
        try:
            candidate.relative_to(blob_root)
        except ValueError:
            return None
        if candidate == blob_root:
            return None
        return candidate

    if not explicit and not dry_run:
        raise PermissionError("gc_requires_explicit_flag_or_dry_run")
    _refresh_purge_eligibility()
    eligible = list(
        StoredBlob.objects.filter(
            retention_class=StoredBlob.RetentionClass.HEAVY,
            lifecycle=StoredBlob.Lifecycle.PURGE_ELIGIBLE,
        )
    )
    tombstones: list[str] = []
    purged = 0
    skipped = 0
    volume = 0
    for blob in eligible:
        if not _blob_still_purge_eligible(blob):
            skipped += 1
            if blob.lifecycle == StoredBlob.Lifecycle.PURGE_ELIGIBLE:
                blob.lifecycle = StoredBlob.Lifecycle.QUARANTINE
                blob.save(update_fields=["lifecycle", "updated_at"])
            continue
        volume += blob.byte_size
        rejection = (
            blob.references.filter(conserving_status=BlobReference.Conserving.REJECTED)
            .order_by("-updated_at")
            .values_list("rejection_reason", flat=True)
            .first()
            or ""
        )
        useful = (blob.metadata or {}).get("useful_excerpt", "")
        payload = {
            "content_hash": blob.content_hash,
            "byte_size": blob.byte_size,
            "media_type": blob.media_type,
            "source_url": blob.source_url,
            "useful_excerpt": useful,
            "timecodes": (blob.metadata or {}).get("timecodes", []),
            "provenance": (blob.metadata or {}).get("provenance", {}),
            "rejection_reason": rejection,
            "dry_run": dry_run,
        }
        if dry_run:
            tombstones.append(blob.content_hash)
            AuditEvent.objects.create(
                event_type="blob_gc_dry_run",
                object_type="StoredBlob",
                object_id=blob.content_hash,
                detail=payload,
            )
            continue
        deleted = False
        storage_path_for_audit = blob.storage_path
        if blob.storage_path:
            if not _blob_still_purge_eligible(blob):
                skipped += 1
                continue
            path = safe_path(blob.storage_path)
            if path is None:
                AuditEvent.objects.create(
                    event_type="blob_gc_path_refused",
                    object_type="StoredBlob",
                    object_id=blob.content_hash,
                    detail={**payload, "storage_path": blob.storage_path},
                )
                skipped += 1
                continue
            if path.is_file():
                if not _blob_still_purge_eligible(blob):
                    skipped += 1
                    continue
                try:
                    _unlink_blob_file(path)
                    deleted = True
                except PermissionError as exc:
                    if str(exc) == "purge_aborted_late_reference":
                        if blob.lifecycle == StoredBlob.Lifecycle.PURGE_ELIGIBLE:
                            blob.lifecycle = StoredBlob.Lifecycle.QUARANTINE
                            blob.save(update_fields=["lifecycle", "updated_at"])
                        AuditEvent.objects.create(
                            event_type="blob_gc_late_reference_abort",
                            object_type="StoredBlob",
                            object_id=blob.content_hash,
                            detail={**payload, "error": str(exc)},
                        )
                        skipped += 1
                        continue
                    AuditEvent.objects.create(
                        event_type="blob_gc_unlink_failed",
                        object_type="StoredBlob",
                        object_id=blob.content_hash,
                        detail={**payload, "error": str(exc)},
                    )
                    skipped += 1
                    continue
                except OSError as exc:
                    AuditEvent.objects.create(
                        event_type="blob_gc_unlink_failed",
                        object_type="StoredBlob",
                        object_id=blob.content_hash,
                        detail={**payload, "error": str(exc)},
                    )
                    skipped += 1
                    continue
            elif path.exists():
                skipped += 1
                continue
            else:
                AuditEvent.objects.create(
                    event_type="blob_gc_tombstoned_absent",
                    object_type="StoredBlob",
                    object_id=blob.content_hash,
                    detail={**payload, "storage_path": storage_path_for_audit, "file_deleted": False},
                )
                blob.lifecycle = StoredBlob.Lifecycle.TOMBSTONED
                blob.storage_path = ""
                blob.save(update_fields=["lifecycle", "storage_path", "updated_at"])
                skipped += 1
                continue
        BlobTombstone.objects.update_or_create(
            content_hash=blob.content_hash,
            defaults={
                "byte_size": blob.byte_size,
                "media_type": blob.media_type,
                "source_url": blob.source_url,
                "useful_excerpt": useful,
                "timecodes": (blob.metadata or {}).get("timecodes", []),
                "provenance": (blob.metadata or {}).get("provenance", {}),
                "rejection_reason": rejection,
                "purged_at": timezone.now(),
                "dry_run": False,
            },
        )
        blob.lifecycle = StoredBlob.Lifecycle.TOMBSTONED
        blob.storage_path = ""
        blob.save(update_fields=["lifecycle", "storage_path", "updated_at"])
        purged += 1
        tombstones.append(blob.content_hash)
        event_type = "blob_gc_purged" if deleted else "blob_gc_tombstoned_absent"
        AuditEvent.objects.create(
            event_type=event_type,
            object_type="StoredBlob",
            object_id=blob.content_hash,
            detail={**payload, "file_deleted": deleted, "storage_path": storage_path_for_audit},
        )
    return GCResult(
        dry_run=dry_run,
        eligible=len(eligible),
        purged=purged,
        tombstones=tombstones,
        skipped_referenced=skipped,
        volume_bytes=volume,
    )


def storage_metrics() -> dict:
    agg = StoredBlob.objects.values("retention_class", "lifecycle").annotate(n=Count("id"), volume=Count("byte_size"))
    # volume réel
    from django.db.models import Sum

    by_class = {
        row["retention_class"]: {
            "count": row["c"],
            "bytes": row["b"] or 0,
        }
        for row in StoredBlob.objects.values("retention_class").annotate(c=Count("id"), b=Sum("byte_size"))
    }
    purge_eligible = StoredBlob.objects.filter(lifecycle=StoredBlob.Lifecycle.PURGE_ELIGIBLE).aggregate(
        c=Count("id"), b=Sum("byte_size")
    )
    return {
        "by_class": by_class,
        "purge_eligible_count": purge_eligible["c"] or 0,
        "purge_eligible_bytes": purge_eligible["b"] or 0,
        "no_political_scores": True,
        "detail_rows": list(agg),
    }
