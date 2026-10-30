"""Ordonnanceur multi-fréquences idempotent (FEEDS-01 / BUILD-002-R1)."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from atelier.models import AuditEvent, FeedCursor, IngestError, JobRun, Source
from atelier.services.canonical import sha256_digest
from atelier.services.feeds.adapters import collect_feed, implementation_label, targets_configured
from atelier.services.feeds.pipeline import continue_detail_work_for_document, normalize_and_ingest
from atelier.services.feeds.registry import DEFAULT_CADENCE_SECONDS, FeedSpec, all_feed_specs
from atelier.services.file_lock import try_acquire_exclusive_nonblocking
from atelier.services.jobs import claim_job, finish_job


logger = logging.getLogger("atelier.feeds.scheduler")
PROCESSOR_VERSION = "dv-permanent-scheduler/5"
MAX_CONSECUTIVE_ERRORS = 5


def _ingest_continuation_due(metrics: dict | None) -> bool:
    metrics = metrics or {}
    if metrics.get("detail_continuation_due") or (
        metrics.get("raw_snapshot_complete") and not metrics.get("detail_work_complete", True)
    ):
        return True
    if metrics.get("snapshot_complete") and metrics.get("detail_work_complete", True):
        return False
    if metrics.get("ingest_continuation_due"):
        return True
    cursor = metrics.get("parse_cursor") or {}
    return bool(cursor.get("snapshot_sha256")) and not cursor.get("complete") and cursor.get("document_id")


def _detail_continuation_due(metrics: dict | None) -> bool:
    """Raw drainé mais file détail encore ouverte."""
    metrics = metrics or {}
    if metrics.get("detail_continuation_due"):
        return True
    raw_ok = bool(metrics.get("raw_snapshot_complete") or metrics.get("snapshot_complete"))
    if not raw_ok:
        return False
    if "detail_work_complete" in metrics:
        return not bool(metrics.get("detail_work_complete"))
    pending = int(metrics.get("detail_pending") or 0)
    retryable = int(metrics.get("detail_retryable") or 0)
    return pending > 0 or retryable > 0


def _network_probe_due(cursor: FeedCursor, now=None) -> bool:
    """True si le sondage réseau est autorisé par la cadence (indépendant de l'ingest)."""
    now = now or timezone.now()
    if cursor.block_reason or cursor.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        return False
    seconds = cadence_seconds(cursor.cadence)
    if seconds <= 0:
        return False
    # capture_success_at privilégié pour la cadence réseau ; fallback last_success_at.
    metrics = cursor.metrics or {}
    last_capture = metrics.get("capture_success_at")
    last_dt = parse_datetime(last_capture) if isinstance(last_capture, str) else None
    if last_dt is not None and timezone.is_naive(last_dt):
        last_dt = timezone.make_aware(last_dt, timezone.utc)
    if last_dt is None:
        last_dt = cursor.last_success_at
    if last_dt and (now - last_dt).total_seconds() < seconds:
        return False
    return True


def cadence_seconds(cadence: str) -> int:
    configured = getattr(settings, "DV_FEED_CADENCE_SECONDS", None) or {}
    return int(configured.get(cadence, DEFAULT_CADENCE_SECONDS.get(cadence, 0)))


def cadence_bucket(cadence: str, now=None) -> str:
    """Bucket déterministe de cadence — permet plusieurs sondages le même jour.

    L'idempotence concurrente porte sur (feed, cadence, bucket), pas sur la
    date civile ni sur l'ancien cursor_token (qui empêchait de re-sonder).
    """
    now = now or timezone.now()
    seconds = cadence_seconds(cadence)
    if seconds <= 0:
        return f"manual:{now.date().isoformat()}"
    bucket = int(now.timestamp()) // max(1, seconds)
    return f"{cadence}:{bucket}"


def ensure_feed_cursors(*, activate_defaults: bool = True) -> int:
    """Amorce les objets absents ; conserve intégralement les décisions persistées.

    Ce n'est pas une requalification : droits, pauses et horodatages existants
    doivent être modifiés par leur parcours explicite, jamais par la couverture.
    """
    created = 0
    for spec in all_feed_specs():
        source = _ensure_source(spec)
        block = spec.block_reason
        # Config media : lever le blocage registry si cibles présentes.
        if (
            block == FeedCursor.BlockReason.CONFIG
            and spec.adapter in {"press_rss", "youtube_meta", "podcast_rss", "bluesky_public"}
            and targets_configured(spec)
        ):
            block = ""
        _, was_created = FeedCursor.objects.get_or_create(
            feed_id=spec.feed_id,
            defaults={
                "source": source,
                "family": spec.family,
                "authority": spec.authority,
                "cadence": spec.cadence,
                "mechanism": spec.mechanism,
                "auth_required": spec.auth_required,
                "ingestion_active": bool(
                    activate_defaults
                    and spec.default_active
                    and not block
                    and source.operational_enabled
                    and source.access_status in {Source.AccessStatus.QUALIFIED, Source.AccessStatus.ACTIVE}
                    and source.rights_status != Source.RightsStatus.FORBIDDEN
                ),
                "coverage_note": spec.coverage_note,
                "block_reason": block,
            },
        )
        if was_created:
            created += 1
    return created


def _ensure_source(spec: FeedSpec) -> Source:
    rights = {
        "open": Source.RightsStatus.OPEN,
        "excerpt_allowed": Source.RightsStatus.EXCERPT_ALLOWED,
        "internal_only": Source.RightsStatus.INTERNAL_ONLY,
        "forbidden": Source.RightsStatus.FORBIDDEN,
        "unknown": Source.RightsStatus.UNKNOWN,
    }.get(spec.rights_status, Source.RightsStatus.UNKNOWN)
    access = Source.AccessStatus.BLOCKED if spec.block_reason else Source.AccessStatus.QUALIFIED
    enabled = bool(spec.default_active and not spec.block_reason)
    source, _ = Source.objects.get_or_create(
        stable_id=spec.feed_id,
        defaults={
            "producer": spec.authority,
            "family": spec.family,
            "discovery_url": spec.discovery_url or "https://www.assemblee-nationale.fr/",
            "document_url": spec.document_url or "",
            "terms_url": spec.terms_url or "",
            "temporal_scope": (spec.historical_scope or spec.coverage_note)[:240],
            "licence_label": "voir terms_url / registry BUILD-002",
            "rights_status": rights,
            "access_status": access,
            "operational_enabled": enabled,
            "candidate_frequency": spec.cadence,
            "qualification_notes": spec.coverage_note,
            "qualified_at": timezone.now(),
        },
    )
    return source


def _execution_block(cursor: FeedCursor, spec: FeedSpec | None) -> str:
    """Restrictions effectives relues sans réécrire les choix persistés."""
    if spec is None:
        return "feed_not_in_registry"
    if spec.rights_status == Source.RightsStatus.FORBIDDEN:
        return "rights_forbidden"
    registry_block = spec.block_reason
    if (
        registry_block == FeedCursor.BlockReason.CONFIG
        and spec.adapter in {"press_rss", "youtube_meta", "podcast_rss", "bluesky_public"}
        and targets_configured(spec)
    ):
        registry_block = ""
    if registry_block:
        return registry_block
    if cursor.block_reason:
        return cursor.block_reason
    if not cursor.ingestion_active:
        return "ingestion_paused"
    source = cursor.source
    if source is None:
        return "source_missing"
    if source.rights_status == Source.RightsStatus.FORBIDDEN:
        return "rights_forbidden"
    if not source.operational_enabled or source.access_status not in {
        Source.AccessStatus.QUALIFIED, Source.AccessStatus.ACTIVE,
    }:
        return "source_not_operational"
    return ""


def due_feeds(cadences: Iterable[str] | None = None, *, now=None) -> list[FeedCursor]:
    """Feeds dus pour sondage réseau OU continuation d'ingest (indépendante)."""
    now = now or timezone.now()
    qs = FeedCursor.objects.filter(ingestion_active=True).select_related("source")
    if cadences:
        qs = qs.filter(cadence__in=list(cadences))
    due = []
    seen = set()
    specs = {spec.feed_id: spec for spec in all_feed_specs()}
    for cursor in qs:
        if cursor.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            continue
        if _execution_block(cursor, specs.get(cursor.feed_id)):
            continue
        metrics = cursor.metrics or {}
        if _ingest_continuation_due(metrics) or _network_probe_due(cursor, now):
            if cursor.feed_id not in seen:
                due.append(cursor)
                seen.add(cursor.feed_id)
    return due


def _apply_pipeline_metrics(metrics: dict, pipeline: dict, *, now, capture_status: str | None = None) -> dict:
    parse_cursor = pipeline.get("parse_cursor") or metrics.get("parse_cursor") or {}
    raw_complete = bool(
        pipeline.get("raw_snapshot_complete")
        if "raw_snapshot_complete" in pipeline
        else (pipeline.get("snapshot_complete") or parse_cursor.get("complete"))
    )
    detail_complete = bool(
        pipeline.get("detail_work_complete")
        if "detail_work_complete" in pipeline
        else True
    )
    metrics = dict(metrics)
    detail_metrics = pipeline.get("detail_metrics") or {}
    metrics.update(
        {
            "last_parse_status": pipeline.get("parse_status"),
            "last_ingest_status": pipeline.get("ingest_status"),
            "last_objects_created": pipeline.get("objects_created", 0),
            "last_objects_updated": pipeline.get("objects_updated", 0),
            "last_declarations_created": pipeline.get("declarations_created", 0),
            "last_relation_candidates_created": pipeline.get("relation_candidates_created", 0),
            "last_object_types": pipeline.get("object_types", []),
            "parse_cursor": parse_cursor,
            "raw_snapshot_complete": raw_complete,
            "snapshot_complete": raw_complete,
            "detail_work_complete": detail_complete,
            "detail_continuation_due": raw_complete and not detail_complete,
            "processed": parse_cursor.get("processed"),
            "total_seen": parse_cursor.get("total_seen"),
            "ingest_continuation_due": (not raw_complete) or (raw_complete and not detail_complete),
            "budget_exhausted": bool(pipeline.get("budget_exhausted")),
            "detail_metrics": detail_metrics,
            "discoveries_seen": pipeline.get("discoveries_seen", metrics.get("discoveries_seen", 0)),
            "details_due": pipeline.get("details_due", 0),
            "details_fetched": pipeline.get("details_fetched", 0),
            "details_unchanged": pipeline.get("details_unchanged", 0),
            "details_failed": pipeline.get("details_failed", 0),
            "details_parsed": pipeline.get("details_parsed", 0),
            "discovery_only_count": pipeline.get("discovery_only_count", 0),
            "detail_pending": pipeline.get("detail_pending", detail_metrics.get("detail_pending", 0)),
            "detail_retryable": pipeline.get("detail_retryable", detail_metrics.get("detail_retryable", 0)),
            "detail_blocked": pipeline.get("detail_blocked", detail_metrics.get("detail_blocked", 0)),
            "evidence_document_bound": bool(
                pipeline.get("evidence_document_bound")
                or detail_metrics.get("evidence_document_bound")
                or metrics.get("evidence_document_bound")
            ),
            "processor_version": PROCESSOR_VERSION,
        }
    )
    if capture_status:
        metrics["last_capture_status"] = capture_status
        if capture_status in {"created", "unchanged", "fixture"}:
            metrics["capture_success_at"] = now.isoformat()
    if raw_complete and detail_complete:
        metrics["ingest_complete_at"] = now.isoformat()
        metrics["detail_complete_at"] = now.isoformat()
        metrics["ingest_continuation_due"] = False
        metrics["detail_continuation_due"] = False
    else:
        metrics.pop("ingest_complete_at", None)
        if not detail_complete:
            metrics.pop("detail_complete_at", None)
        if raw_complete:
            metrics["raw_complete_at"] = metrics.get("raw_complete_at") or now.isoformat()
    return metrics


@transaction.atomic
def continue_detail_work(cursor: FeedCursor) -> dict:
    """Continue la file détail d'un snapshot déjà drainé — sans re-fetch d'index."""
    cursor = FeedCursor.objects.select_for_update().get(pk=cursor.pk)
    now = timezone.now()
    cursor.last_attempt_at = now
    metrics = dict(cursor.metrics or {})
    parse_cursor = dict(metrics.get("parse_cursor") or {})
    document_id = parse_cursor.get("document_id") or metrics.get("last_document_id")
    sha = (parse_cursor.get("snapshot_sha256") or "")[:16] or "unknown"
    pending = int(metrics.get("detail_pending") or 0)
    retryable = int(metrics.get("detail_retryable") or 0)
    terminal = int(metrics.get("detail_terminal") or 0) or (
        int(metrics.get("detail_parsed") or 0)
        + int(metrics.get("detail_incomplete") or 0)
        + int(metrics.get("detail_blocked") or 0)
    )
    input_digest = sha256_digest(
        {
            "feed_id": cursor.feed_id,
            "purpose": "detail_continuation",
            "snapshot": sha,
            "pending": pending,
            "retryable": retryable,
            "terminal": terminal,
            "processor": PROCESSOR_VERSION,
        }
    )
    job, _ = JobRun.objects.get_or_create(
        task_name="permanent_feed_detail_continuation",
        batch_id=f"{cursor.feed_id}:detail:{sha}:t{terminal}:p{pending}:r{retryable}",
        input_digest=input_digest,
        processor_version=PROCESSOR_VERSION,
        defaults={"configuration": {"feed_id": cursor.feed_id, "document_id": document_id}},
    )
    if job.state == JobRun.State.SUCCEEDED and metrics.get("detail_work_complete"):
        cursor.save(update_fields=["last_attempt_at", "updated_at"])
        return {
            "feed_id": cursor.feed_id,
            "status": "cached",
            "capture_status": "skipped_detail_continuation",
            "parse_status": "cached",
            "ingest_status": "cached",
            "snapshot_complete": bool(parse_cursor.get("complete")),
            "detail_work_complete": True,
            "job_id": str(job.job_id),
        }
    if job.state == JobRun.State.SUCCEEDED and not metrics.get("detail_work_complete"):
        # Progression nulle possible (backoff) : nouveau job pour ne pas rester bloqué.
        input_digest = sha256_digest(
            {
                "feed_id": cursor.feed_id,
                "purpose": "detail_continuation_retry",
                "snapshot": sha,
                "pending": pending,
                "at": now.isoformat(),
                "processor": PROCESSOR_VERSION,
            }
        )
        job = JobRun.objects.create(
            task_name="permanent_feed_detail_continuation",
            batch_id=f"{cursor.feed_id}:detail:{sha}:retry:{now.strftime('%Y%m%d%H%M%S')}",
            input_digest=input_digest,
            processor_version=PROCESSOR_VERSION,
            configuration={"feed_id": cursor.feed_id, "document_id": document_id},
        )
    claimed = claim_job(job.job_id, lease_seconds=900)
    if claimed is None:
        return {
            "feed_id": cursor.feed_id,
            "status": "busy",
            "capture_status": "skipped_detail_continuation",
        }

    if not document_id:
        finish_job(job.job_id, succeeded=False, error_code="detail_continuation_document_missing")
        metrics["detail_continuation_due"] = False
        cursor.metrics = metrics
        cursor.last_error_code = "detail_continuation_document_missing"
        cursor.save()
        return {
            "feed_id": cursor.feed_id,
            "status": "error",
            "capture_status": "skipped_detail_continuation",
            "error_code": "detail_continuation_document_missing",
        }

    spec = next((s for s in all_feed_specs() if s.feed_id == cursor.feed_id), None)
    parser_id = (spec.adapter if spec else "") or ""
    pipeline = continue_detail_work_for_document(
        feed_id=cursor.feed_id,
        document_id=int(document_id),
        parser_id=parser_id,
        execution_mode=cursor.execution_mode or "live",
    )
    parse_status = pipeline.get("parse_status", "ok")
    ingest_status = pipeline.get("ingest_status", "ok")
    if parse_status == "error":
        cursor.consecutive_errors += 1
        cursor.last_error_code = pipeline.get("error_code") or "detail_error"
        finish_job(job.job_id, succeeded=False, error_code=cursor.last_error_code)
    else:
        cursor.consecutive_errors = 0
        cursor.last_error_code = ""
        if pipeline.get("detail_work_complete"):
            cursor.last_success_at = now
        finish_job(job.job_id, succeeded=True)

    # Conserve le parse_cursor raw déjà complet.
    pipeline = {
        **pipeline,
        "parse_cursor": {
            **parse_cursor,
            "complete": True,
            "document_id": document_id,
        },
        "raw_snapshot_complete": True,
        "snapshot_complete": True,
    }
    metrics = _apply_pipeline_metrics(metrics, pipeline, now=now)
    metrics["last_capture_status"] = "skipped_detail_continuation"
    metrics["implementation"] = implementation_label(spec) if spec else "fixture_only"
    cursor.metrics = metrics
    cursor.save()
    AuditEvent.objects.create(
        event_type="permanent_feed_detail_continuation",
        object_type="FeedCursor",
        object_id=cursor.feed_id,
        detail={
            "parse_status": parse_status,
            "ingest_status": ingest_status,
            "pipeline": pipeline,
            "publication_authorized": False,
        },
    )
    return {
        "feed_id": cursor.feed_id,
        "status": "detail_continuation",
        "capture_status": "skipped_detail_continuation",
        "parse_status": parse_status,
        "ingest_status": ingest_status,
        "objects_created": pipeline.get("objects_created", 0),
        "declarations_created": pipeline.get("declarations_created", 0),
        "snapshot_complete": True,
        "raw_snapshot_complete": True,
        "detail_work_complete": bool(pipeline.get("detail_work_complete")),
        "job_id": str(job.job_id),
    }


@transaction.atomic
def continue_ingest(cursor: FeedCursor) -> dict:
    """Continue le parsing/ingestion d'un snapshot déjà capturé — sans re-fetch."""
    cursor = FeedCursor.objects.select_for_update().get(pk=cursor.pk)
    now = timezone.now()
    cursor.last_attempt_at = now
    metrics = dict(cursor.metrics or {})
    parse_cursor = dict(metrics.get("parse_cursor") or {})
    document_id = parse_cursor.get("document_id") or metrics.get("last_document_id")
    sha = (parse_cursor.get("snapshot_sha256") or "")[:16] or "unknown"
    offset = int(parse_cursor.get("offset") or 0)
    input_digest = sha256_digest(
        {
            "feed_id": cursor.feed_id,
            "purpose": "ingest_continuation",
            "snapshot": sha,
            "offset": offset,
            "processor": PROCESSOR_VERSION,
        }
    )
    job, _ = JobRun.objects.get_or_create(
        task_name="permanent_feed_ingest_continuation",
        batch_id=f"{cursor.feed_id}:ingest:{sha}:{offset}",
        input_digest=input_digest,
        processor_version=PROCESSOR_VERSION,
        defaults={"configuration": {"feed_id": cursor.feed_id, "offset": offset}},
    )
    if job.state == JobRun.State.SUCCEEDED:
        cursor.save(update_fields=["last_attempt_at", "updated_at"])
        return {
            "feed_id": cursor.feed_id,
            "status": "cached",
            "capture_status": "skipped_continuation",
            "parse_status": "cached",
            "ingest_status": "cached",
            "snapshot_complete": bool(parse_cursor.get("complete")),
            "job_id": str(job.job_id),
        }
    claimed = claim_job(job.job_id, lease_seconds=900)
    if claimed is None:
        return {
            "feed_id": cursor.feed_id,
            "status": "busy",
            "capture_status": "skipped_continuation",
        }

    if not document_id:
        finish_job(job.job_id, succeeded=False, error_code="continuation_document_missing")
        metrics["ingest_continuation_due"] = False
        cursor.metrics = metrics
        cursor.last_error_code = "continuation_document_missing"
        cursor.save()
        return {
            "feed_id": cursor.feed_id,
            "status": "error",
            "capture_status": "skipped_continuation",
            "error_code": "continuation_document_missing",
        }

    spec = next((s for s in all_feed_specs() if s.feed_id == cursor.feed_id), None)
    parser_id = (spec.adapter if spec else "") or ""
    pipeline = normalize_and_ingest(
        feed_id=cursor.feed_id,
        document_id=int(document_id),
        parser_id=parser_id,
        execution_mode=cursor.execution_mode or "live",
        parse_cursor=parse_cursor,
        drain=True,
    )
    parse_status = pipeline.get("parse_status", "ok")
    ingest_status = pipeline.get("ingest_status", "ok")
    if parse_status == "error":
        cursor.consecutive_errors += 1
        cursor.last_error_code = pipeline.get("error_code") or "parse_error"
        finish_job(job.job_id, succeeded=False, error_code=cursor.last_error_code)
    else:
        cursor.consecutive_errors = 0
        cursor.last_error_code = ""
        # last_success_at : raw + détail complets (ne masque pas un partiel détail).
        if pipeline.get("snapshot_complete") and pipeline.get("detail_work_complete", True):
            cursor.last_success_at = now
        finish_job(job.job_id, succeeded=True)

    metrics = _apply_pipeline_metrics(metrics, pipeline, now=now)
    metrics["last_capture_status"] = "skipped_continuation"
    metrics["implementation"] = implementation_label(spec) if spec else "fixture_only"
    cursor.metrics = metrics
    cursor.save()
    AuditEvent.objects.create(
        event_type="permanent_feed_ingest_continuation",
        object_type="FeedCursor",
        object_id=cursor.feed_id,
        detail={
            "parse_status": parse_status,
            "ingest_status": ingest_status,
            "pipeline": pipeline,
            "publication_authorized": False,
        },
    )
    return {
        "feed_id": cursor.feed_id,
        "status": "continuation",
        "capture_status": "skipped_continuation",
        "parse_status": parse_status,
        "ingest_status": ingest_status,
        "objects_created": pipeline.get("objects_created", 0),
        "objects_updated": pipeline.get("objects_updated", 0),
        "declarations_created": pipeline.get("declarations_created", 0),
        "relation_candidates_created": pipeline.get("relation_candidates_created", 0),
        "snapshot_complete": bool(pipeline.get("snapshot_complete")),
        "parse_cursor": pipeline.get("parse_cursor"),
        "job_id": str(job.job_id),
    }


@transaction.atomic
def run_feed(cursor: FeedCursor) -> dict:
    cursor = FeedCursor.objects.select_for_update().get(pk=cursor.pk)
    spec = next((s for s in all_feed_specs() if s.feed_id == cursor.feed_id), None)
    block = _execution_block(cursor, spec)
    if block:
        return {
            "feed_id": cursor.feed_id,
            "status": "blocked",
            "capture_status": "blocked",
            "execution_mode": FeedCursor.ExecutionMode.BLOCKED,
            "error_code": block,
        }
    now = timezone.now()
    metrics_now = cursor.metrics or {}
    continuation = _ingest_continuation_due(metrics_now)
    detail_only = _detail_continuation_due(metrics_now)
    network_due = _network_probe_due(cursor, now)
    # Si seule la continuation détail est due : pas de re-fetch d'index.
    if detail_only and not network_due:
        return continue_detail_work(cursor)
    # Si seule la continuation raw est due : pas de re-fetch.
    # Si le sondage réseau est aussi dû : prioriser le probe (détecte supersession).
    if continuation and not network_due and not detail_only:
        return continue_ingest(cursor)
    # Raw incomplet + détail : continue_ingest (qui traitera aussi des détails via pipeline).
    if continuation and not network_due:
        return continue_ingest(cursor)

    cursor.last_attempt_at = now
    # Bucket de cadence : un feed fast/hourly peut re-sonder plusieurs fois/jour.
    # Ne pas figer l'idempotence sur cursor_token/ETag avant le check réseau.
    bucket = cadence_bucket(cursor.cadence, now)
    input_digest = sha256_digest(
        {
            "feed_id": cursor.feed_id,
            "cadence_bucket": bucket,
            "cadence": cursor.cadence,
            "processor": PROCESSOR_VERSION,
            "purpose": "conditional_probe",
        }
    )
    job, _ = JobRun.objects.get_or_create(
        task_name="permanent_feed_collection",
        batch_id=f"{cursor.feed_id}:{bucket}",
        input_digest=input_digest,
        processor_version=PROCESSOR_VERSION,
        defaults={"configuration": {"feed_id": cursor.feed_id, "cadence": cursor.cadence, "bucket": bucket}},
    )
    if job.state == JobRun.State.SUCCEEDED:
        # Même bucket déjà traité (anti-doublon concurrent) — pas un cache journalier.
        cursor.save(update_fields=["last_attempt_at", "updated_at"])
        return {
            "feed_id": cursor.feed_id,
            "status": "cached",
            "capture_status": "cached",
            "parse_status": "cached",
            "ingest_status": "cached",
            "cadence_bucket": bucket,
            "job_id": str(job.job_id),
        }

    claimed = claim_job(job.job_id, lease_seconds=900)
    if claimed is None:
        return {
            "feed_id": cursor.feed_id,
            "status": "busy",
            "capture_status": "busy",
            "cadence_bucket": bucket,
        }

    result = collect_feed(cursor.feed_id, cursor_token=cursor.cursor_token, etag=cursor.etag)
    capture_status = result.status
    parse_status = "skipped"
    ingest_status = "skipped"
    pipeline = {
        "objects_created": 0,
        "objects_updated": 0,
        "declarations_created": 0,
        "relation_candidates_created": 0,
    }

    cursor.last_error_code = result.error_code
    cursor.execution_mode = {
        "live": FeedCursor.ExecutionMode.LIVE,
        "fixture": FeedCursor.ExecutionMode.FIXTURE,
        "blocked": FeedCursor.ExecutionMode.BLOCKED,
    }.get(result.execution_mode, FeedCursor.ExecutionMode.CONFIGURED_NOT_RUN)
    cursor.last_run_provenance = result.execution_mode

    metrics = dict(cursor.metrics or {})
    parse_cursor = dict(metrics.get("parse_cursor") or {})
    snapshot_transition = None

    if result.status in {"created", "unchanged", "fixture"}:
        digest = result.payload_digest or ""
        if digest and parse_cursor.get("snapshot_sha256") and parse_cursor.get("snapshot_sha256") != digest:
            # Nouveau snapshot pendant un ancien incomplet → reset explicite, jamais mélange.
            snapshot_transition = {
                "kind": "superseded_incomplete",
                "previous_sha256": parse_cursor.get("snapshot_sha256"),
                "previous_processed": parse_cursor.get("processed"),
                "previous_complete": parse_cursor.get("complete"),
                "new_sha256": digest,
            }
            parse_cursor = {
                "snapshot_sha256": digest,
                "document_id": result.document_id,
                "offset": 0,
                "processed": 0,
                "total_seen": None,
                "complete": False,
                "supersedes": snapshot_transition["previous_sha256"],
            }
        elif digest and not parse_cursor.get("snapshot_sha256"):
            parse_cursor = {
                "snapshot_sha256": digest,
                "document_id": result.document_id,
                "offset": 0,
                "processed": 0,
                "total_seen": None,
                "complete": False,
            }
        elif result.document_id and not parse_cursor.get("document_id"):
            parse_cursor["document_id"] = result.document_id

        need_ingest = True
        if result.status == "unchanged" and parse_cursor.get("complete"):
            need_ingest = False
            parse_status = "skipped_complete"
            ingest_status = "skipped_complete"
            pipeline["snapshot_complete"] = True

        if need_ingest and (result.document_id or result.raw_items is not None):
            # Drain sous budget dans le même run — pas d'attente de cadence quotidienne.
            pipeline = normalize_and_ingest(
                feed_id=cursor.feed_id,
                document_id=result.document_id,
                parser_id=result.parser_id,
                raw_items=result.raw_items,
                execution_mode=result.execution_mode,
                parse_cursor=parse_cursor,
                drain=True,
            )
            parse_status = pipeline.get("parse_status", "ok")
            ingest_status = pipeline.get("ingest_status", "ok")
            if pipeline.get("parse_cursor"):
                parse_cursor = pipeline["parse_cursor"]
                if result.document_id:
                    parse_cursor["document_id"] = result.document_id
        if parse_status == "error":
            cursor.consecutive_errors += 1
            cursor.last_error_code = pipeline.get("error_code") or "parse_error"
            finish_job(job.job_id, succeeded=False, error_code=cursor.last_error_code)
            IngestError.objects.create(
                feed_id=cursor.feed_id,
                stage="parse",
                error_code=cursor.last_error_code,
                detail=pipeline,
                attempt=cursor.consecutive_errors,
                next_retry_at=now + timedelta(minutes=min(60, 2 ** min(cursor.consecutive_errors, 6))),
            )
        else:
            cursor.consecutive_errors = 0
            metrics["capture_success_at"] = now.isoformat()
            metrics["last_document_id"] = result.document_id
            # last_success_at seulement si ingest raw+détail complet.
            if (pipeline.get("snapshot_complete") and pipeline.get("detail_work_complete", True)) or not need_ingest:
                cursor.last_success_at = now
                if pipeline.get("detail_work_complete", True):
                    metrics["ingest_complete_at"] = now.isoformat()
            if result.execution_mode == "live":
                cursor.last_live_success_at = now
            elif result.execution_mode == "fixture":
                cursor.last_fixture_success_at = now
            if result.etag:
                cursor.etag = result.etag
            if result.last_modified:
                cursor.last_modified = result.last_modified
            if result.cursor_token:
                cursor.cursor_token = result.cursor_token
            if cursor.source_id:
                Source.objects.filter(pk=cursor.source_id).update(
                    last_success_at=now,
                    last_failure_code="",
                    access_status=Source.AccessStatus.ACTIVE,
                )
            finish_job(job.job_id, succeeded=True)
    elif result.status == "blocked":
        cursor.block_reason = result.error_code or cursor.block_reason or FeedCursor.BlockReason.CREDENTIALS
        cursor.ingestion_active = False
        cursor.execution_mode = FeedCursor.ExecutionMode.BLOCKED
        finish_job(job.job_id, succeeded=False, error_code=result.error_code)
        IngestError.objects.create(
            feed_id=cursor.feed_id,
            stage="collect",
            error_code=result.error_code or "blocked",
            detail=result.detail or {},
            max_attempts=1,
            resolved_at=now,
        )
    else:
        cursor.consecutive_errors += 1
        cursor.last_error_code = result.error_code or "error"
        if cursor.source_id:
            Source.objects.filter(pk=cursor.source_id).update(
                last_failure_at=now,
                last_failure_code=cursor.last_error_code,
            )
        finish_job(job.job_id, succeeded=False, error_code=cursor.last_error_code)
        IngestError.objects.create(
            feed_id=cursor.feed_id,
            stage="collect",
            error_code=cursor.last_error_code,
            detail=result.detail or {},
            attempt=cursor.consecutive_errors,
            next_retry_at=now + timedelta(minutes=min(60, 2 ** min(cursor.consecutive_errors, 6))),
        )

    if result.status in {"created", "unchanged", "fixture"} and parse_status != "error":
        metrics = _apply_pipeline_metrics(
            metrics, {**pipeline, "parse_cursor": parse_cursor}, now=now, capture_status=capture_status
        )
    else:
        metrics.update(
            {
                "last_capture_status": capture_status,
                "last_parse_status": parse_status,
                "last_ingest_status": ingest_status,
                "last_digest": result.payload_digest,
                "cadence_bucket": bucket,
                "parse_cursor": parse_cursor,
            }
        )
    if snapshot_transition:
        metrics["snapshot_transition"] = snapshot_transition
    metrics["last_digest"] = result.payload_digest
    metrics["cadence_bucket"] = bucket
    metrics["implementation"] = implementation_label(
        next(s for s in all_feed_specs() if s.feed_id == cursor.feed_id)
    )
    cursor.metrics = metrics
    cursor.save()
    AuditEvent.objects.create(
        event_type="permanent_feed_run",
        object_type="FeedCursor",
        object_id=cursor.feed_id,
        detail={
            "capture_status": capture_status,
            "parse_status": parse_status,
            "ingest_status": ingest_status,
            "execution_mode": result.execution_mode,
            "cadence_bucket": bucket,
            "pipeline": pipeline,
            "snapshot_transition": snapshot_transition,
            "publication_authorized": False,
        },
    )
    return {
        "feed_id": cursor.feed_id,
        "status": result.status,
        "capture_status": capture_status,
        "parse_status": parse_status,
        "ingest_status": ingest_status,
        "error_code": result.error_code,
        "objects_created": pipeline.get("objects_created", 0),
        "objects_updated": pipeline.get("objects_updated", 0),
        "declarations_created": pipeline.get("declarations_created", 0),
        "relation_candidates_created": pipeline.get("relation_candidates_created", 0),
        "execution_mode": result.execution_mode,
        "cadence_bucket": bucket,
        "snapshot_complete": bool(parse_cursor.get("complete")),
        "parse_cursor": parse_cursor,
        "ingest_continuation_due": bool(metrics.get("ingest_continuation_due")),
        "detail_metrics": pipeline.get("detail_metrics") or {},
        "job_id": str(job.job_id),
    }


def run_cadence_once(cadences: Iterable[str], *, use_lock: bool = True, seed_only: bool = False) -> dict:
    cadences = list(cadences)
    lock_path = settings.RUNTIME_DIR / "permanent-scheduler.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _run():
        created = ensure_feed_cursors()
        if seed_only:
            return {"success": True, "results": [], "cadences": [], "feeds_created": created}
        results = []
        ok = True
        for cursor in due_feeds(cadences):
            try:
                results.append(run_feed(cursor))
            except Exception as exc:  # noqa: BLE001
                ok = False
                logger.exception("feed_run_failed feed=%s", cursor.feed_id)
                results.append(
                    {
                        "feed_id": cursor.feed_id,
                        "status": "error",
                        "capture_status": "error",
                        "error_code": type(exc).__name__,
                    }
                )
        return {"success": ok, "results": results, "cadences": list(cadences)}

    if not use_lock:
        return _run()
    with lock_path.open("a+") as lock:
        if not try_acquire_exclusive_nonblocking(lock):
            return {"success": None, "skipped": "lock_held", "results": []}
        return _run()


def coverage_matrix() -> list[dict]:
    from atelier.services.feeds.official_formats import live_format_parser_implemented

    rows = []
    specs = {s.feed_id: s for s in all_feed_specs()}
    for cursor in FeedCursor.objects.select_related("source").order_by("authority", "feed_id"):
        spec = specs.get(cursor.feed_id)
        impl = implementation_label(spec) if spec else "fixture_only"
        metrics = cursor.metrics or {}
        adapter_name = spec.adapter if spec else ""
        live_http = bool(metrics.get("live_http_verified"))
        live_parse = bool(metrics.get("live_parse_verified"))
        live_ingest = bool(metrics.get("live_ingest_verified"))
        # live_checked = parse+ingest live prouvés, pas seulement HTTP
        live_checked = bool(metrics.get("live_checked")) and live_parse and live_ingest
        rows.append(
            {
                "feed_id": cursor.feed_id,
                "authority": cursor.authority,
                "family": cursor.family,
                "mechanism": cursor.mechanism,
                "rights": cursor.source.rights_status if cursor.source_id else "unknown",
                "auth_required": cursor.auth_required,
                "cadence": cursor.cadence,
                "implementation": impl,
                "adapter_implemented": impl in {"live_adapter", "config_blocked"}
                or (impl == "live_adapter"),
                "live_format_parser_implemented": live_format_parser_implemented(adapter_name),
                "configured": bool(
                    cursor.ingestion_active
                    or (spec and not spec.block_reason)
                    or (spec and targets_configured(spec))
                ),
                "live_http_verified": live_http,
                "live_parse_verified": live_parse,
                "live_ingest_verified": live_ingest,
                "live_checked": live_checked,
                "live_checked_means": "live_parse_and_ingest_verified_not_http_only",
                "execution_mode": cursor.execution_mode,
                "last_success_at": cursor.last_success_at.isoformat() if cursor.last_success_at else None,
                "last_live_success_at": cursor.last_live_success_at.isoformat()
                if cursor.last_live_success_at
                else None,
                "last_fixture_success_at": cursor.last_fixture_success_at.isoformat()
                if cursor.last_fixture_success_at
                else None,
                "last_run_provenance": cursor.last_run_provenance or None,
                "capture_ok": metrics.get("last_capture_status") in {"created", "unchanged", "fixture", "cached"},
                "parse_ok": metrics.get("last_parse_status") in {"ok", "cached"},
                "ingest_ok": metrics.get("last_ingest_status") in {"ok", "cached"},
                "object_types": metrics.get("last_object_types") or [],
                "cursor": cursor.cursor_token,
                "coverage": cursor.coverage_note,
                "ingestion_active": cursor.ingestion_active,
                "block_reason": cursor.block_reason or (spec.block_reason if spec else None) or None,
                "external_or_config_block": cursor.block_reason
                or (spec.block_reason if spec else "")
                or None,
                "snapshot_complete": bool(metrics.get("snapshot_complete")),
                "raw_snapshot_complete": bool(
                    metrics.get("raw_snapshot_complete", metrics.get("snapshot_complete"))
                ),
                "detail_work_complete": bool(metrics.get("detail_work_complete", True))
                if "detail_work_complete" in metrics or metrics.get("detail_pending") is not None
                else True,
                "detail_pending": metrics.get("detail_pending"),
                "detail_retryable": metrics.get("detail_retryable"),
                "detail_blocked": metrics.get("detail_blocked"),
                "detail_complete": bool(metrics.get("detail_work_complete")),
                "evidence_document_bound": bool(metrics.get("evidence_document_bound")),
                "capture_success_at": metrics.get("capture_success_at"),
                "ingest_complete_at": metrics.get("ingest_complete_at"),
                "raw_complete_at": metrics.get("raw_complete_at"),
                "detail_complete_at": metrics.get("detail_complete_at"),
                "processed": metrics.get("processed"),
                "total_seen": metrics.get("total_seen"),
                "parse_cursor_offset": (metrics.get("parse_cursor") or {}).get("offset"),
                "ingest_continuation_due": bool(metrics.get("ingest_continuation_due")),
                "detail_continuation_due": bool(metrics.get("detail_continuation_due")),
                "discoveries_seen": metrics.get("discoveries_seen"),
                "details_due": metrics.get("details_due"),
                "details_fetched": metrics.get("details_fetched"),
                "details_unchanged": metrics.get("details_unchanged"),
                "details_failed": metrics.get("details_failed"),
                "details_parsed": metrics.get("details_parsed"),
                "detail_progress": metrics.get("detail_metrics") or {},
                "discovery_only_last": metrics.get("discovery_only_count"),
                "processor_version": metrics.get("processor_version") or PROCESSOR_VERSION,
                "tests": f"test_build002_r5::{cursor.feed_id}",
            }
        )
    return rows
