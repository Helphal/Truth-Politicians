"""File persistante discovery → détail (BUILD-002-R5)."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any
from urllib.parse import urldefrag, urlsplit, urlunsplit

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from atelier.models import DetailWorkItem, DocumentVersion
from atelier.services.feeds.detail_fetch import detail_max_per_run, resolve_detail
from atelier.services.feeds.official_formats import extract_items_from_bytes


def canonicalize_detail_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    raw, _frag = urldefrag(raw)
    parts = urlsplit(raw)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Normalise schéma/host en minuscules ; conserve query.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def work_key(*, feed_id: str, parent_document_id: int | None, parser_id: str, canonical_url: str) -> str:
    material = f"{feed_id}|{parent_document_id or 0}|{parser_id}|{canonical_url}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def enqueue_discoveries(
    items: list[dict],
    *,
    feed_id: str,
    parser_id: str,
    parent_document: DocumentVersion | None,
    execution_mode: str,
) -> tuple[list[DetailWorkItem], int]:
    """Enqueue chaque discovery ; retourne (créés_ou_existants, discoveries_seen)."""
    seen = 0
    created_or_found: list[DetailWorkItem] = []
    parent_id = parent_document.pk if parent_document else None
    for item in items:
        if not (item.get("discovery_only") or item.get("item_kind") == "discovery"):
            continue
        seen += 1
        url = canonicalize_detail_url(item.get("source_url") or "")
        if not url:
            continue
        key = work_key(
            feed_id=feed_id,
            parent_document_id=parent_id,
            parser_id=parser_id,
            canonical_url=url,
        )
        obj, was_created = DetailWorkItem.objects.get_or_create(
            work_key=key,
            defaults={
                "feed_id": feed_id,
                "parser_id": parser_id,
                "canonical_url": url[:1000],
                "parent_document": parent_document,
                "title": (item.get("title") or "")[:500],
                "status": DetailWorkItem.Status.PENDING,
                "execution_mode": execution_mode,
                "meta": {
                    "locator": item.get("locator") or "",
                    "parent_discovery_url": item.get("source_url") or url,
                },
            },
        )
        if not was_created and obj.status == DetailWorkItem.Status.PENDING:
            # Rafraîchir titre / mode si encore pending
            updates = []
            title = (item.get("title") or "")[:500]
            if title and obj.title != title:
                obj.title = title
                updates.append("title")
            if execution_mode and obj.execution_mode != execution_mode:
                obj.execution_mode = execution_mode
                updates.append("execution_mode")
            if updates:
                obj.save(update_fields=[*updates, "updated_at"])
        created_or_found.append(obj)
    return created_or_found, seen


def detail_queue_counts(feed_id: str, *, parent_document_id: int | None = None) -> dict[str, int]:
    qs = DetailWorkItem.objects.filter(feed_id=feed_id)
    if parent_document_id is not None:
        qs = qs.filter(parent_document_id=parent_document_id)
    rows = qs.values("status").annotate(n=Count("id"))
    by_status = {r["status"]: r["n"] for r in rows}
    pending = by_status.get(DetailWorkItem.Status.PENDING, 0)
    retryable = by_status.get(DetailWorkItem.Status.RETRYABLE_ERROR, 0)
    blocked = by_status.get(DetailWorkItem.Status.BLOCKED_PERMANENT, 0)
    parsed = by_status.get(DetailWorkItem.Status.PARSED, 0)
    incomplete = by_status.get(DetailWorkItem.Status.INCOMPLETE, 0)
    fetched = by_status.get(DetailWorkItem.Status.FETCHED, 0)
    total = sum(by_status.values())
    terminal = parsed + incomplete + blocked
    return {
        "detail_pending": pending,
        "detail_retryable": retryable,
        "detail_blocked": blocked,
        "detail_parsed": parsed,
        "detail_incomplete": incomplete,
        "detail_fetched": fetched,
        "detail_total": total,
        "detail_terminal": terminal,
        "detail_complete": int(total > 0 and terminal == total and pending == 0 and retryable == 0 and fetched == 0),
        "detail_work_complete": bool(total == 0 or (terminal == total and pending == 0 and retryable == 0 and fetched == 0)),
    }


def due_detail_items(feed_id: str, *, parent_document_id: int | None = None, limit: int = 10) -> list[DetailWorkItem]:
    now = timezone.now()
    qs = DetailWorkItem.objects.filter(feed_id=feed_id).filter(
        Q(status=DetailWorkItem.Status.PENDING)
        | Q(status=DetailWorkItem.Status.RETRYABLE_ERROR, next_attempt_at__isnull=True)
        | Q(status=DetailWorkItem.Status.RETRYABLE_ERROR, next_attempt_at__lte=now)
        | Q(status=DetailWorkItem.Status.FETCHED)
    )
    if parent_document_id is not None:
        qs = qs.filter(parent_document_id=parent_document_id)
    return list(qs.order_by("id")[:limit])


def _mark_terminal(item: DetailWorkItem, status: str, *, error: str = "", detail_document_id: int | None = None):
    item.status = status
    item.last_error_code = error
    item.completed_at = timezone.now()
    item.next_attempt_at = None
    if detail_document_id:
        item.detail_document_id = detail_document_id
    item.save(
        update_fields=[
            "status",
            "last_error_code",
            "completed_at",
            "next_attempt_at",
            "detail_document",
            "updated_at",
        ]
    )


def _mark_retryable(item: DetailWorkItem, error: str):
    item.status = DetailWorkItem.Status.RETRYABLE_ERROR
    item.last_error_code = error[:120]
    item.last_attempt_at = timezone.now()
    if item.attempt_count >= item.max_attempts:
        _mark_terminal(item, DetailWorkItem.Status.BLOCKED_PERMANENT, error=f"max_attempts:{error}")
        return
    backoff = min(3600, (2 ** min(item.attempt_count, 6)) * 30)
    item.next_attempt_at = timezone.now() + timedelta(seconds=backoff)
    item.save(
        update_fields=[
            "status",
            "last_error_code",
            "last_attempt_at",
            "next_attempt_at",
            "updated_at",
        ]
    )


@transaction.atomic
def process_detail_queue(
    *,
    feed_id: str,
    parser_id: str,
    execution_mode: str,
    parent_document: DocumentVersion | None = None,
    max_details: int | None = None,
) -> dict[str, Any]:
    """Traite jusqu'à max_details tâches dues ; retourne items parole + métriques."""
    limit = detail_max_per_run() if max_details is None else max_details
    parent_id = parent_document.pk if parent_document else None
    due = due_detail_items(feed_id, parent_document_id=parent_id, limit=limit)
    metrics = {
        "details_due": len(due),
        "details_fetched": 0,
        "details_unchanged": 0,
        "details_fixture": 0,
        "details_failed": 0,
        "details_blocked": 0,
        "details_parsed": 0,
        "details_incomplete": 0,
        "details_retryable": 0,
    }
    speech_items: list[dict] = []

    for item in due:
        item.last_attempt_at = timezone.now()
        item.attempt_count = int(item.attempt_count or 0) + 1
        item.execution_mode = execution_mode or item.execution_mode
        item.save(update_fields=["last_attempt_at", "attempt_count", "execution_mode", "updated_at"])

        resolved = resolve_detail(
            item.canonical_url,
            feed_id=feed_id,
            parser_id=parser_id or item.parser_id,
            parent_document=item.parent_document or parent_document,
            title=item.title,
            execution_mode=execution_mode,
        )

        if resolved.status == "blocked":
            metrics["details_blocked"] += 1
            _mark_terminal(
                item,
                DetailWorkItem.Status.BLOCKED_PERMANENT,
                error=resolved.error_code or "blocked",
            )
            continue

        if resolved.status == "failed" or not resolved.raw:
            metrics["details_failed"] += 1
            metrics["details_retryable"] += 1
            _mark_retryable(item, resolved.error_code or "detail_fetch_failed")
            continue

        if resolved.status == "unchanged":
            metrics["details_unchanged"] += 1
        elif resolved.status == "fixture":
            metrics["details_fixture"] += 1
        else:
            metrics["details_fetched"] += 1

        item.status = DetailWorkItem.Status.FETCHED
        item.detail_document_id = resolved.document_id
        item.save(update_fields=["status", "detail_document", "updated_at"])

        detail_items = extract_items_from_bytes(
            resolved.raw,
            parser_id or item.parser_id,
            media_type=resolved.media_type or "text/html",
            source_url=resolved.url or item.canonical_url,
            limit=None,
            mode="production",
        ).get("items") or []
        metrics["details_parsed"] += 1

        for d in detail_items:
            d.setdefault("parent_discovery_url", item.canonical_url)
            if resolved.document_id:
                d["detail_document_id"] = resolved.document_id
                d["evidence_document_id"] = resolved.document_id

        speechish = [
            d
            for d in detail_items
            if d.get("exact_quote") and not d.get("discovery_only") and not d.get("incomplete")
        ]
        if speechish:
            speech_items.extend(speechish)
            _mark_terminal(
                item,
                DetailWorkItem.Status.PARSED,
                detail_document_id=resolved.document_id,
            )
        else:
            metrics["details_incomplete"] += 1
            _mark_terminal(
                item,
                DetailWorkItem.Status.INCOMPLETE,
                error="speaker_or_quote_missing",
                detail_document_id=resolved.document_id,
            )

    counts = detail_queue_counts(feed_id, parent_document_id=parent_id)
    return {
        "speech_items": speech_items,
        "metrics": {**metrics, **counts},
        "detail_work_complete": counts["detail_work_complete"],
    }
