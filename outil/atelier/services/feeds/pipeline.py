"""Normalisation capture → objets métier / paroles (BUILD-002-R4)."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from atelier.models import (
    AuditEvent,
    DocumentVersion,
    IndexedPassage,
    LegislativeDossier,
    Organization,
    Person,
    ReferenceResolution,
    RelationCandidate,
    Source,
    StagedScrutiny,
    TextVersion,
)
from atelier.services.canonical import sha256_digest
from atelier.services.feeds.detail_fetch import detail_max_per_run
from atelier.services.feeds.detail_queue import (
    detail_queue_counts,
    enqueue_discoveries,
    process_detail_queue,
)
from atelier.services.feeds.official_formats import (
    PRODUCTION_CHUNK_SIZE,
    extract_items_from_bytes,
    extract_items_from_document,
)
from atelier.services.feeds.registry import feed_by_id
from atelier.services.ingest import ingest_speech_batch
from atelier.services.speech import SpeechCandidate


PROCESSOR = "dv-feed-pipeline/5"


def _ingest_budget_objects() -> int:
    return int(getattr(settings, "DV_FEED_INGEST_BUDGET_OBJECTS", 5000))


def _ingest_budget_seconds() -> float:
    return float(getattr(settings, "DV_FEED_INGEST_BUDGET_SECONDS", 120))


def _ingest_max_chunks() -> int:
    return int(getattr(settings, "DV_FEED_INGEST_MAX_CHUNKS", 50))


def _load_payload(
    document: DocumentVersion | None,
    raw_items: list[dict] | None,
    *,
    parser_id: str = "",
    offset: int = 0,
    limit: int | None = None,
    mode: str = "production",
) -> dict[str, Any]:
    if raw_items is not None and (raw_items or document is None):
        items = list(raw_items or [])
        total = len(items)
        if limit is None:
            chunk = items[offset:]
        else:
            chunk = items[offset : offset + limit]
        return {
            "items": chunk,
            "source": "adapter_raw_items",
            "total_seen": total,
            "complete": offset + len(chunk) >= total,
            "next_offset": offset + len(chunk),
            "processed": offset + len(chunk),
        }
    if document is None:
        return {"items": [], "total_seen": 0, "complete": True, "next_offset": offset, "processed": offset}
    receipt = document.collection_receipt or {}
    if receipt.get("execution_mode") == "fixture" or document.parser_version.startswith("fixture:"):
        items = list(raw_items or [])
        total = len(items)
        if limit is None:
            chunk = items[offset:]
        else:
            chunk = items[offset : offset + limit]
        return {
            "items": chunk,
            "source": "fixture_document",
            "total_seen": total,
            "complete": offset + len(chunk) >= total,
            "next_offset": offset + len(chunk),
            "processed": offset + len(chunk),
        }
    # Compromis ZIP : chaque chunk relit les octets DocumentVersion puis applique
    # offset/limit en mémoire. Pas de cache parse partagé entre runs (évite mélange
    # de snapshots) ; le curseur évite de ré-ingérer les items déjà traités.
    extracted = extract_items_from_document(
        document,
        parser_id or "",
        limit=limit if limit is not None else PRODUCTION_CHUNK_SIZE,
        offset=offset,
        mode=mode,
    )
    if extracted.get("parse_status") == "error":
        raise ValueError(extracted.get("error_code") or extracted.get("error_message") or "official_parse_error")
    return {
        "items": extracted.get("items") or [],
        "source": "official_format",
        "format": extracted.get("format"),
        "parser_version": extracted.get("parser_version"),
        "captured_bytes": document.byte_size,
        "errors": extracted.get("errors") or [],
        "total_seen": extracted.get("total_seen"),
        "complete": extracted.get("complete"),
        "next_offset": extracted.get("next_offset"),
        "processed": extracted.get("processed"),
    }


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(value[:31], fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _empty_detail_metrics() -> dict[str, int]:
    return {
        "discoveries_seen": 0,
        "details_due": 0,
        "details_fetched": 0,
        "details_unchanged": 0,
        "details_failed": 0,
        "details_blocked": 0,
        "details_parsed": 0,
        "details_incomplete": 0,
        "details_fixture": 0,
        "details_retryable": 0,
        "detail_enqueued": 0,
    }


_DETAIL_COUNTER_KEYS = frozenset(
    {
        "discoveries_seen",
        "detail_enqueued",
        "details_due",
        "details_fetched",
        "details_unchanged",
        "details_failed",
        "details_blocked",
        "details_parsed",
        "details_incomplete",
        "details_fixture",
        "details_retryable",
    }
)


def _merge_detail_metrics(acc: dict[str, int], chunk: dict) -> None:
    """Cumule les compteurs d'action ; remplace les états de file par le dernier snapshot."""
    for k, v in chunk.items():
        if isinstance(v, bool):
            acc[k] = int(v)
        elif isinstance(v, int):
            if k in _DETAIL_COUNTER_KEYS:
                acc[k] = int(acc.get(k, 0)) + v
            else:
                acc[k] = v


def _resolve_discovery_details(
    items: list[dict],
    *,
    parser_id: str,
    feed_id: str,
    execution_mode: str,
    parent_document: DocumentVersion | None,
    max_details: int | None = None,
) -> tuple[list[dict], dict[str, int], list[dict]]:
    """Enqueue discoveries dans la file persistante, puis traite un budget de détails."""
    metrics = _empty_detail_metrics()
    non_discovery: list[dict] = []
    discovery_items: list[dict] = []
    for item in items:
        if item.get("discovery_only") or item.get("item_kind") == "discovery":
            discovery_items.append(item)
        else:
            # Parole déjà présente (Syceron, etc.) : rattache le document capturé parent.
            if parent_document is not None and not item.get("evidence_document_id") and not item.get("detail_document_id"):
                item = {
                    **item,
                    "evidence_document_id": parent_document.pk,
                    "detail_document_id": parent_document.pk,
                }
            non_discovery.append(item)

    enqueued, seen = enqueue_discoveries(
        discovery_items,
        feed_id=feed_id,
        parser_id=parser_id,
        parent_document=parent_document,
        execution_mode=execution_mode,
    )
    metrics["discoveries_seen"] = seen
    metrics["detail_enqueued"] = len(enqueued)

    processed = process_detail_queue(
        feed_id=feed_id,
        parser_id=parser_id,
        execution_mode=execution_mode,
        parent_document=parent_document,
        max_details=max_details,
    )
    _merge_detail_metrics(metrics, processed.get("metrics") or {})

    expanded = list(non_discovery)
    expanded.extend(processed.get("speech_items") or [])
    # Les discoveries ne sont plus renvoyées comme detail_deferred jetable :
    # elles vivent dans DetailWorkItem.
    return expanded, metrics, discovery_items


def _ingest_detail_speech_items(
    speech_items: list[dict],
    *,
    source: Source,
    document: DocumentVersion | None,
    feed_id: str,
    parser_id: str,
) -> dict[str, Any]:
    """Persiste les paroles issues de la file détail (sans re-parser l'index)."""
    if not speech_items:
        return {
            "objects_created": 0,
            "objects_updated": 0,
            "declarations_created": 0,
            "relation_candidates_created": 0,
            "discovery_only_count": 0,
            "incomplete_count": 0,
            "object_types": [],
        }
    return _normalize_speech(
        speech_items,
        source=source,
        document=document,
        feed_id=feed_id,
        parser_id=parser_id,
    )


def continue_detail_work_for_document(
    *,
    feed_id: str,
    document_id: int | None,
    parser_id: str,
    execution_mode: str = "live",
    max_details: int | None = None,
) -> dict[str, Any]:
    """Traite la file détail sans re-fetch ni re-parse de l'index."""
    document = DocumentVersion.objects.filter(pk=document_id).select_related("source").first() if document_id else None
    source = document.source if document else Source.objects.filter(stable_id=feed_id).first()
    if source is None:
        raise RuntimeError("pipeline_source_missing")
    processed = process_detail_queue(
        feed_id=feed_id,
        parser_id=parser_id,
        execution_mode=execution_mode,
        parent_document=document,
        max_details=max_details,
    )
    speech_result = _ingest_detail_speech_items(
        processed.get("speech_items") or [],
        source=source,
        document=document,
        feed_id=feed_id,
        parser_id=parser_id,
    )
    counts = detail_queue_counts(feed_id, parent_document_id=document.pk if document else None)
    metrics = {**_empty_detail_metrics(), **(processed.get("metrics") or {}), **counts}
    return {
        "parse_status": "ok",
        "ingest_status": "ok" if counts.get("detail_work_complete") else "partial",
        "processor": PROCESSOR,
        "execution_mode": execution_mode,
        "raw_snapshot_complete": True,
        "snapshot_complete": True,
        "detail_work_complete": bool(counts.get("detail_work_complete")),
        "detail_continuation_due": not bool(counts.get("detail_work_complete")),
        "objects_created": speech_result.get("objects_created", 0),
        "objects_updated": speech_result.get("objects_updated", 0),
        "declarations_created": speech_result.get("declarations_created", 0),
        "relation_candidates_created": speech_result.get("relation_candidates_created", 0),
        "discovery_only_count": speech_result.get("discovery_only_count", 0),
        "incomplete_count": speech_result.get("incomplete_count", 0),
        "object_types": speech_result.get("object_types") or [],
        "detail_metrics": metrics,
        **metrics,
        "parse_cursor": {
            "document_id": document_id,
            "complete": True,
            "detail_only_continuation": True,
        },
    }


@transaction.atomic
def normalize_and_ingest(
    *,
    feed_id: str,
    document_id: int | None,
    parser_id: str,
    raw_items: list[dict] | None = None,
    execution_mode: str = "fixture",
    parse_cursor: dict | None = None,
    chunk_size: int | None = None,
    drain: bool = False,
    max_chunks: int | None = None,
    budget_objects: int | None = None,
    budget_seconds: float | None = None,
    max_details: int | None = None,
) -> dict[str, Any]:
    """
    Parse + persistance, éventuellement par chunks.

    drain=True : enchaîne les chunks jusqu'à complete ou épuisement du budget
    (objets / secondes / max_chunks). Un snapshot partiel reste
    snapshot_complete=false avec curseur reprisable sans re-fetch.
    """
    spec = feed_by_id(feed_id)
    document = DocumentVersion.objects.filter(pk=document_id).select_related("source").first() if document_id else None
    source = document.source if document else Source.objects.filter(stable_id=feed_id).first()
    if source is None:
        raise RuntimeError("pipeline_source_missing")

    cursor = dict(parse_cursor or {})
    offset = int(cursor.get("offset") or 0)
    size = chunk_size or PRODUCTION_CHUNK_SIZE
    mode = "production" if execution_mode in {"live", "fixture"} else "probe"
    chunks_limit = _ingest_max_chunks() if max_chunks is None else max_chunks
    objects_budget = _ingest_budget_objects() if budget_objects is None else budget_objects
    seconds_budget = _ingest_budget_seconds() if budget_seconds is None else budget_seconds
    started = time.monotonic()

    totals = {
        "objects_created": 0,
        "objects_updated": 0,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "discovery_only_count": 0,
        "incomplete_count": 0,
    }
    detail_metrics = _empty_detail_metrics()
    object_types: list[str] = []
    last_payload_meta: dict[str, Any] = {}
    chunks_done = 0
    budget_exhausted = False

    try:
        while True:
            payload = _load_payload(
                document,
                raw_items,
                parser_id=parser_id or spec.adapter,
                offset=offset,
                limit=size,
                mode=mode,
            )
            items = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(items, list):
                items = []
            items, chunk_detail, discovery_items = _resolve_discovery_details(
                items,
                parser_id=parser_id or spec.adapter,
                feed_id=feed_id,
                execution_mode=execution_mode,
                parent_document=document,
                max_details=max_details,
            )
            _merge_detail_metrics(detail_metrics, chunk_detail)

            # Indexe les titres de discovery sur le document parent (pas de Declaration).
            if discovery_items and document:
                for idx, ditem in enumerate(discovery_items):
                    title = (ditem.get("title") or ditem.get("source_url") or "").strip()
                    if not title:
                        continue
                    IndexedPassage.objects.update_or_create(
                        document=document,
                        extractor_version=PROCESSOR,
                        ordinal=20_000 + offset + idx,
                        defaults={
                            "locator": (ditem.get("locator") or f"discovery:{idx}")[:500],
                            "visible_text": title[:5000],
                            "search_text": title[:5000].casefold(),
                            "text_sha256": sha256_digest(title),
                        },
                    )
                totals["discovery_only_count"] += len(discovery_items)

            result = _dispatch_parser(
                parser_id or spec.adapter,
                items=items,
                source=source,
                document=document,
                feed_id=feed_id,
            )
            for key in ("objects_created", "objects_updated", "declarations_created", "relation_candidates_created"):
                totals[key] += int(result.get(key) or 0)
            totals["discovery_only_count"] += int(result.get("discovery_only_count") or 0)
            totals["incomplete_count"] += int(result.get("incomplete_count") or 0)
            for ot in result.get("object_types") or []:
                if ot not in object_types:
                    object_types.append(ot)

            # Progression curseur = items bruts du snapshot (pas les détails étendus)
            raw_chunk_len = len(payload.get("items") or [])
            total_seen = payload.get("total_seen")
            next_offset = int(payload.get("next_offset") or (offset + raw_chunk_len))
            complete = bool(payload.get("complete"))
            cursor = {
                "snapshot_sha256": cursor.get("snapshot_sha256")
                or (document.sha256 if document else "")
                or "",
                "document_id": document_id,
                "offset": next_offset,
                "processed": next_offset,
                "total_seen": total_seen,
                "complete": complete and (isinstance(total_seen, int) and next_offset >= total_seen or total_seen is None and complete),
                "remaining": (total_seen - next_offset) if isinstance(total_seen, int) else None,
            }
            if isinstance(total_seen, int):
                cursor["complete"] = next_offset >= total_seen
            last_payload_meta = {
                "payload_source": payload.get("source"),
                "official_format": payload.get("format"),
                "official_parser_version": payload.get("parser_version"),
            }
            offset = next_offset
            chunks_done += 1

            if cursor.get("complete"):
                break
            if not drain:
                break
            if raw_chunk_len == 0:
                cursor["complete"] = True
                break
            if chunks_done >= chunks_limit:
                budget_exhausted = True
                break
            if totals["objects_created"] + totals["objects_updated"] + totals["declarations_created"] >= objects_budget:
                budget_exhausted = True
                break
            if (time.monotonic() - started) >= seconds_budget:
                budget_exhausted = True
                break

        # Après drainage raw : un passage détail supplémentaire sous le budget restant
        # (pas de drainage illimité — la reprise passe par continue_detail_work).
        raw_snapshot_complete = bool(cursor.get("complete"))
        parent_id = document.pk if document else None
        queue_counts = detail_queue_counts(feed_id, parent_document_id=parent_id)
        detail_metrics.update({k: int(v) if isinstance(v, bool) else v for k, v in queue_counts.items()})

        details_spent = int(detail_metrics.get("details_fetched", 0)) + int(
            detail_metrics.get("details_unchanged", 0)
        ) + int(detail_metrics.get("details_fixture", 0)) + int(
            detail_metrics.get("details_failed", 0)
        ) + int(detail_metrics.get("details_blocked", 0))
        detail_cap = detail_max_per_run() if max_details is None else max_details
        remaining_detail_budget = max(0, detail_cap - details_spent)

        if (
            raw_snapshot_complete
            and remaining_detail_budget > 0
            and not bool(queue_counts.get("detail_work_complete"))
            and int(queue_counts.get("detail_pending", 0)) + int(queue_counts.get("detail_retryable", 0))
            + int(queue_counts.get("detail_fetched", 0))
            > 0
        ):
            # Si le chunk raw n'avait aucune discovery, process_detail_queue n'a peut‑être
            # pas encore été appelé (ex. reprise détail-only via normalize). Traite un budget.
            if details_spent == 0 or (drain and details_spent < detail_cap):
                # Évite un double traitement dans le même appel quand _resolve a déjà
                # consommé tout le budget.
                if details_spent == 0:
                    processed = process_detail_queue(
                        feed_id=feed_id,
                        parser_id=parser_id or spec.adapter,
                        execution_mode=execution_mode,
                        parent_document=document,
                        max_details=remaining_detail_budget,
                    )
                    _merge_detail_metrics(detail_metrics, processed.get("metrics") or {})
                    speech_result = _ingest_detail_speech_items(
                        processed.get("speech_items") or [],
                        source=source,
                        document=document,
                        feed_id=feed_id,
                        parser_id=parser_id or spec.adapter,
                    )
                    for key in (
                        "objects_created",
                        "objects_updated",
                        "declarations_created",
                        "relation_candidates_created",
                    ):
                        totals[key] += int(speech_result.get(key) or 0)
                    totals["incomplete_count"] += int(speech_result.get("incomplete_count") or 0)
                    for ot in speech_result.get("object_types") or []:
                        if ot not in object_types:
                            object_types.append(ot)
                    queue_counts = detail_queue_counts(feed_id, parent_document_id=parent_id)
                    detail_metrics.update(
                        {k: int(v) if isinstance(v, bool) else v for k, v in queue_counts.items()}
                    )

        detail_work_complete = bool(queue_counts.get("detail_work_complete"))
        fully_complete = raw_snapshot_complete and detail_work_complete
        out = {
            **totals,
            "parse_status": "ok",
            "ingest_status": "ok" if fully_complete else "partial",
            "processor": PROCESSOR,
            "execution_mode": execution_mode,
            "object_types": object_types,
            "parse_cursor": cursor,
            "raw_snapshot_complete": raw_snapshot_complete,
            # Compat R4 : snapshot_complete = raw drainé (indépendant des détails).
            "snapshot_complete": raw_snapshot_complete,
            "detail_work_complete": detail_work_complete,
            "detail_continuation_due": raw_snapshot_complete and not detail_work_complete,
            "budget_exhausted": budget_exhausted,
            "chunks_processed": chunks_done,
            "detail_metrics": detail_metrics,
            **detail_metrics,
            **last_payload_meta,
        }
        AuditEvent.objects.create(
            event_type="feed_pipeline_ingest",
            object_type="FeedCursor",
            object_id=feed_id,
            detail={k: v for k, v in out.items() if k != "declarations"},
        )
        return out
    except Exception as exc:  # noqa: BLE001
        AuditEvent.objects.create(
            event_type="feed_pipeline_parse_error",
            object_type="FeedCursor",
            object_id=feed_id,
            detail={"error": type(exc).__name__, "message": str(exc)[:400]},
        )
        return {
            "parse_status": "error",
            "ingest_status": "skipped",
            "error_code": type(exc).__name__,
            "objects_created": totals["objects_created"],
            "objects_updated": totals["objects_updated"],
            "declarations_created": totals["declarations_created"],
            "relation_candidates_created": totals["relation_candidates_created"],
            "parse_cursor": cursor,
            "snapshot_complete": False,
            "detail_metrics": detail_metrics,
            **detail_metrics,
        }


def _dispatch_parser(parser_id: str, *, items, source, document, feed_id: str) -> dict[str, Any]:
    if parser_id in {"an_amo", "senat_acteurs"}:
        return _normalize_actors(items, source=source, document=document, institution=source.producer)
    if parser_id in {"an_votes", "senat_scrutins"}:
        return _normalize_votes(items, source=source, document=document)
    if parser_id in {"an_dossiers", "senat_dosleg", "senat_akn"}:
        return _normalize_dossiers(items, source=source, document=document)
    if parser_id in {"an_amendements", "senat_ameli"}:
        return _normalize_amendments(items, source=source, document=document)
    if parser_id in {
        "an_cr",
        "an_questions",
        "senat_cr",
        "senat_questions",
        "elysee_speech",
        "gouv_speech",
        "press_rss",
        "youtube_meta",
        "podcast_rss",
        "bluesky_public",
    }:
        return _normalize_speech(items, source=source, document=document, feed_id=feed_id, parser_id=parser_id)
    return _normalize_generic(items, source=source, document=document, feed_id=feed_id)


def _normalize_actors(items, *, source, document, institution: str) -> dict[str, Any]:
    created = updated = 0
    for item in items:
        uid = str(item.get("uid") or item.get("id") or item.get("matricule") or "")
        if not uid:
            continue
        display = (
            item.get("display_name")
            or " ".join(filter(None, [item.get("prenom"), item.get("nom")]))
            or item.get("nom")
            or uid
        )
        stable = f"person:{uid}"
        person, was_created = Person.objects.update_or_create(
            stable_id=stable,
            defaults={
                "display_name": display,
                "institutional_ids": {"primary": uid},
                "identity_status": Person.IdentityStatus.RESOLVED,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
        for org_item in item.get("organes") or item.get("groupes") or []:
            label = org_item.get("libelle") or org_item.get("name") or ""
            if not label:
                continue
            Organization.objects.get_or_create(
                stable_id=f"org:{sha256_digest(label.casefold())[:16]}",
                defaults={"name": label, "kind": Organization.Kind.PARLIAMENTARY_GROUP},
            )
        if document:
            ReferenceResolution.objects.get_or_create(
                document=document,
                kind="person",
                institutional_ref=uid,
                defaults={
                    "locator": f"actor:{uid}",
                    "previous": {},
                    "proposed": {"person_stable_id": person.stable_id, "institution": institution},
                    "status": "proposed",
                    "reason": "feed_pipeline_actor",
                },
            )
    return {
        "ingest_status": "ok",
        "objects_created": created,
        "objects_updated": updated,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "object_types": ["Person", "Organization", "ReferenceResolution"],
    }


def _normalize_votes(items, *, source, document) -> dict[str, Any]:
    created = updated = 0
    if document is None:
        return {
            "ingest_status": "error",
            "error_code": "document_required",
            "objects_created": 0,
            "objects_updated": 0,
            "declarations_created": 0,
            "relation_candidates_created": 0,
        }
    for item in items:
        uid = str(item.get("uid") or item.get("source_uid") or item.get("numero") or "")
        if not uid:
            continue
        normalized = {
            "title": item.get("titre") or item.get("title") or "",
            "date": item.get("dateScrutin") or item.get("date") or "",
            "synthese": item.get("syntheseVote") or item.get("counts") or {},
            "nominatif": not bool((item.get("ventilationVotes") or {}).get("nonNominatif", False)),
        }
        digest = sha256_digest(normalized)
        _, was_created = StagedScrutiny.objects.update_or_create(
            document=document,
            source_uid=uid[:160],
            defaults={
                "institution": source.producer,
                "locator": f"scrutiny:{uid}",
                "raw_sha256": digest,
                "normalized": normalized,
                "qualification": "exact_text_not_linked",
            },
        )
        created += int(was_created)
        updated += int(not was_created)
    return {
        "ingest_status": "ok",
        "objects_created": created,
        "objects_updated": updated,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "object_types": ["StagedScrutiny"],
    }


def _normalize_dossiers(items, *, source, document) -> dict[str, Any]:
    created = updated = 0
    for item in items:
        dossier_id = str(item.get("dossier_id") or item.get("uid") or item.get("id") or "")
        if not dossier_id:
            continue
        title = item.get("titre") or item.get("title") or dossier_id
        url = item.get("url") or source.document_url or source.discovery_url
        dossier, was_created = LegislativeDossier.objects.update_or_create(
            stable_id=f"dossier:{dossier_id}"[:180],
            defaults={
                "institution": source.producer,
                "official_title": title,
                "official_url": url,
                "status": item.get("status") or "captured",
            },
        )
        created += int(was_created)
        updated += int(not was_created)
        for idx, etape in enumerate(item.get("etapes") or []):
            TextVersion.objects.update_or_create(
                stable_id=f"text:{dossier_id}:{etape}"[:200],
                defaults={
                    "dossier": dossier,
                    "kind": str(etape),
                    "official_title": f"{title} — {etape}",
                    "version_label": str(etape),
                    "stage": str(etape),
                    "relation": TextVersion.Relation.OTHER,
                    "official_url": url,
                    "source_document": document,
                },
            )
            _ = idx
    return {
        "ingest_status": "ok",
        "objects_created": created,
        "objects_updated": updated,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "object_types": ["LegislativeDossier", "TextVersion"],
    }


def _normalize_amendments(items, *, source, document) -> dict[str, Any]:
    created = 0
    if document is None:
        return {
            "ingest_status": "ok",
            "objects_created": 0,
            "objects_updated": 0,
            "declarations_created": 0,
            "relation_candidates_created": 0,
            "object_types": [],
        }
    for item in items:
        uid = str(item.get("uid") or item.get("numero") or item.get("id") or "")
        if not uid:
            continue
        text = (item.get("objet") or item.get("title") or item.get("texte") or uid).strip()
        digest = sha256_digest(text)
        _, was_created = IndexedPassage.objects.update_or_create(
            document=document,
            extractor_version=PROCESSOR,
            ordinal=created,
            defaults={
                "locator": f"amendement:{uid}"[:500],
                "visible_text": text[:5000],
                "search_text": text[:5000].casefold(),
                "text_sha256": digest,
            },
        )
        created += int(was_created)
    return {
        "ingest_status": "ok",
        "objects_created": created,
        "objects_updated": 0,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "object_types": ["IndexedPassage"],
    }


def _speech_candidates_from_items(items, *, feed_id: str, parser_id: str) -> tuple[list[SpeechCandidate], int, int]:
    out: list[SpeechCandidate] = []
    discovery_only = 0
    incomplete = 0
    for item in items:
        if item.get("discovery_only") or item.get("item_kind") == "discovery":
            discovery_only += 1
            continue
        if item.get("incomplete") or item.get("item_kind") == "metadata_only":
            incomplete += 1
            continue
        quote = (
            item.get("exact_quote")
            or item.get("transcript")
            or item.get("excerpt")
            or item.get("text")
            or ""
        ).strip()
        # Interdit : inventer une citation depuis un titre d'index.
        if not quote:
            incomplete += 1
            continue
        speaker = item.get("speaker_label") or item.get("auteur") or item.get("orateur") or item.get("handle") or ""
        if not speaker:
            incomplete += 1
            continue
        occurred = _parse_date(item.get("occurred_on") or item.get("published") or item.get("date"))
        occurred_state = item.get("occurred_on_state") or ("known" if occurred else "unknown")
        stable = item.get("stable_id") or f"decl:{feed_id}:{sha256_digest(quote + speaker)[:20]}"
        medium = {
            "press_rss": "press",
            "youtube_meta": "video",
            "podcast_rss": "radio",
            "bluesky_public": "social",
            "elysee_speech": "institutional",
            "gouv_speech": "institutional",
        }.get(parser_id, "institutional")
        source_role = item.get("source_role") or ("relay" if parser_id == "press_rss" else "original")
        if parser_id == "youtube_meta" and not item.get("transcript"):
            if not item.get("allow_title_as_quote"):
                incomplete += 1
                continue
        evidence_id = item.get("evidence_document_id") or item.get("detail_document_id")
        if evidence_id is not None:
            try:
                evidence_id = int(evidence_id)
            except (TypeError, ValueError):
                evidence_id = None
        out.append(
            SpeechCandidate(
                stable_id=str(stable)[:200],
                speaker_label=str(speaker)[:250],
                capacity=item.get("capacity") or parser_id,
                exact_quote=quote,
                occurred_on=occurred,
                occurred_on_state=occurred_state,
                date_precision="day" if occurred else "unknown",
                medium=medium,
                source_role=source_role,
                context=item.get("context") or item.get("title") or "",
                source_url=item.get("source_url") or item.get("url") or "",
                locator=item.get("locator") or item.get("timecode") or item.get("post_id") or feed_id,
                organization_label=item.get("organization_label") or "",
                person_stable_id=item.get("person_stable_id"),
                verification_status="feed_pipeline_candidate",
                evidence_document_id=evidence_id,
                extra={
                    "relay_acknowledged": source_role == "relay",
                    "transcript_missing": parser_id == "youtube_meta" and not item.get("transcript"),
                    "full_video_retained": False,
                },
            )
        )
    return out, discovery_only, incomplete


def _normalize_speech(items, *, source, document, feed_id: str, parser_id: str) -> dict[str, Any]:
    # Persiste les discovery comme passages indexés (pas Declaration).
    discovery_count = 0
    if document:
        for idx, item in enumerate(items):
            if not (item.get("discovery_only") or item.get("item_kind") == "discovery"):
                continue
            title = (item.get("title") or item.get("source_url") or "").strip()
            if not title:
                continue
            IndexedPassage.objects.update_or_create(
                document=document,
                extractor_version=PROCESSOR,
                ordinal=10_000 + idx,
                defaults={
                    "locator": (item.get("locator") or f"discovery:{idx}")[:500],
                    "visible_text": title[:5000],
                    "search_text": title[:5000].casefold(),
                    "text_sha256": sha256_digest(title),
                },
            )
            discovery_count += 1

    candidates, discovery_only, incomplete = _speech_candidates_from_items(
        items, feed_id=feed_id, parser_id=parser_id
    )
    before_rc = RelationCandidate.objects.count()
    batch = ingest_speech_batch(candidates, source=source) if candidates else {"created": 0, "skipped": 0}
    after_rc = RelationCandidate.objects.count()
    for idx, cand in enumerate(candidates[:50]):
        # IndexedPassage sur le document détail (preuve), pas sur l'index parent.
        passage_doc = None
        if cand.evidence_document_id:
            passage_doc = DocumentVersion.objects.filter(pk=cand.evidence_document_id).first()
        if passage_doc is None:
            passage_doc = document
        if passage_doc is None:
            continue
        IndexedPassage.objects.update_or_create(
            document=passage_doc,
            extractor_version=PROCESSOR,
            ordinal=idx,
            defaults={
                "locator": (cand.locator or cand.stable_id)[:500],
                "visible_text": cand.exact_quote[:5000],
                "search_text": cand.exact_quote[:5000].casefold(),
                "text_sha256": sha256_digest(cand.exact_quote),
            },
        )
    return {
        "ingest_status": "ok",
        "objects_created": batch.get("created", 0),
        "objects_updated": batch.get("skipped", 0),
        "declarations_created": batch.get("created", 0),
        "relation_candidates_created": max(0, after_rc - before_rc),
        "discovery_only_count": discovery_only + discovery_count,
        "incomplete_count": incomplete,
        "object_types": ["Declaration", "RelationCandidate", "IndexedPassage"],
    }


def _normalize_generic(items, *, source, document, feed_id: str) -> dict[str, Any]:
    if any((item.get("exact_quote") or item.get("excerpt") or item.get("text")) for item in items):
        return _normalize_speech(items, source=source, document=document, feed_id=feed_id, parser_id="generic")
    created = 0
    if document:
        for idx, item in enumerate(items):
            uid = str(item.get("uid") or item.get("id") or idx)
            text = json.dumps(item, ensure_ascii=False)[:5000]
            IndexedPassage.objects.update_or_create(
                document=document,
                extractor_version=PROCESSOR,
                ordinal=idx,
                defaults={
                    "locator": f"item:{uid}"[:500],
                    "visible_text": text,
                    "search_text": text.casefold(),
                    "text_sha256": sha256_digest(text),
                },
            )
            created += 1
    return {
        "ingest_status": "ok",
        "objects_created": created,
        "objects_updated": 0,
        "declarations_created": 0,
        "relation_candidates_created": 0,
        "object_types": ["IndexedPassage"],
    }
