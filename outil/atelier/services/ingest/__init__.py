"""Chaîne unifiée discovery → candidats (INGEST/RECONCILE-01)."""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from atelier.models import (
    AuditEvent,
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    Measure,
    RelationCandidate,
    Source,
)
from atelier.services.canonical import sha256_digest
from atelier.services.speech import SpeechCandidate, persist_speech_candidate
from atelier.services.thematic_integrity import link_matches_current_material
from atelier.services.thematic_qualification import record_declaration_measure_link


PROCESSOR = "dv-ingest-reconcile/1"


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def content_fingerprint(text: str, url: str = "") -> str:
    return sha256_digest({"text": text.strip(), "url": canonicalize_url(url)})


@transaction.atomic
def ingest_speech_batch(
    candidates: Iterable[SpeechCandidate],
    *,
    source: Source,
) -> dict:
    """Déduplication URL+hash, persistance, candidats de relations (non qualifiés)."""
    seen_hashes: set[str] = set()
    created = 0
    skipped = 0
    declarations: list[Declaration] = []
    for candidate in candidates:
        digest = candidate.ensure_hash()
        canon = canonicalize_url(candidate.source_url)
        if digest in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(digest)
        existing = Declaration.objects.filter(stable_id=candidate.stable_id).first()
        if existing is None and canon and candidate.exact_quote:
            existing = (
                Declaration.objects.filter(
                    source_passage__public_url=canon,
                    exact_quote=candidate.exact_quote,
                )
                .order_by("pk")
                .first()
            )
        if existing is not None:
            skipped += 1
            declarations.append(existing)
            continue
        if not candidate.stable_id:
            candidate.stable_id = f"decl:{digest[:24]}"
        decl = persist_speech_candidate(candidate, source=source)
        if decl:
            created += 1
            declarations.append(decl)
            propose_measure_candidates(decl)
    return {
        "created": created,
        "skipped": skipped,
        "declarations": len(declarations),
        "processor": PROCESSOR,
    }


def propose_measure_candidates(declaration: Declaration, *, limit: int = 5) -> list[RelationCandidate]:
    """Propose des candidats texte↔mesure sans aucune qualification automatique."""
    tokens = {t.casefold() for t in declaration.exact_quote.split() if len(t) > 4}
    if not tokens:
        return []
    produced = []
    for measure in Measure.objects.all()[:200]:
        hay = f"{measure.title} {measure.concrete_effect}".casefold()
        overlap = sum(1 for t in tokens if t in hay)
        if overlap < 2:
            continue
        stable_id = f"rc:dm:{declaration.stable_id}:{measure.stable_id}"[:220]
        candidate, was_created = RelationCandidate.objects.get_or_create(
            stable_id=stable_id,
            defaults={
                "kind": RelationCandidate.Kind.DECLARATION_MEASURE,
                "left_object_type": "Declaration",
                "left_object_id": declaration.stable_id,
                "right_object_type": "Measure",
                "right_object_id": measure.stable_id,
                "rationale": (
                    f"chevauchement lexical faible ({overlap} jetons) — "
                    "candidat non qualifié ; revue humaine obligatoire"
                ),
                "score_hint": float(overlap),
                "evidence": {"overlap": overlap, "auto_qualified": False},
                "processor_version": PROCESSOR,
            },
        )
        if was_created:
            produced.append(candidate)
        if len(produced) >= limit:
            break
    return produced


def promote_candidate_requires_human(candidate: RelationCandidate) -> None:
    """Interdit la promotion silencieuse en lien qualifié."""
    raise PermissionError("relation_candidate_requires_explicit_human_qualification")


@transaction.atomic
def nocturnal_reconcile() -> dict:
    """
    Réconciliation nocturne : détecte matière qualifiée périmée (FP-3) et
    ouvre une révision candidate pour réexamen. Ne contourne pas l'immuabilité.
    """
    stale = 0
    examined = 0
    affected_declaration_ids: set[str] = set()
    # Dernière révision par (declaration, measure)
    seen: set[tuple[int, int]] = set()
    rows = (
        DeclarationMeasureLink.objects.filter(
            qualification_status=DocumentaryQualification.QUALIFIED,
        )
        .select_related("declaration", "measure", "evidence", "declaration__source_passage__document")
        .order_by("declaration_id", "measure_id", "-revision")
    )
    for link in rows:
        key = (link.declaration_id, link.measure_id)
        if key in seen:
            continue
        seen.add(key)
        examined += 1
        if link_matches_current_material(link):
            continue
        # Nouvelle révision candidate : la dernière n'est plus « qualified ».
        record_declaration_measure_link(
            user=None,
            declaration=link.declaration,
            measure=link.measure,
            stance=link.stance,
            qualification_status=DocumentaryQualification.CANDIDATE,
            evidence=link.evidence,
            rationale=(
                "Révision ouverte par réconciliation nocturne BUILD-002 : "
                "empreinte FP-3 périmée ; requalification humaine obligatoire."
            ),
        )
        stale += 1
        affected_declaration_ids.add(link.declaration.stable_id)
        AuditEvent.objects.create(
            event_type="nocturnal_fp3_invalidation",
            object_type="DeclarationMeasureLink",
            object_id=str(link.pk),
            detail={
                "declaration": link.declaration.stable_id,
                "measure": link.measure.stable_id,
                "previous_revision": link.revision,
                "action": "opened_candidate_revision",
            },
        )

    dismissed = 0
    for candidate in RelationCandidate.objects.filter(status=RelationCandidate.Status.PROPOSED):
        if candidate.left_object_type == "Declaration":
            if not Declaration.objects.filter(stable_id=candidate.left_object_id).exists():
                candidate.status = RelationCandidate.Status.DISMISSED
                candidate.save(update_fields=["status", "updated_at"])
                dismissed += 1

    AuditEvent.objects.create(
        event_type="nocturnal_reconcile",
        object_type="system",
        object_id=timezone.now().date().isoformat(),
        detail={
            "examined_links": examined,
            "stale": stale,
            "dismissed_candidates": dismissed,
            "affected_declarations": sorted(affected_declaration_ids),
            "incremental_only": True,
        },
    )
    return {
        "examined_links": examined,
        "stale": stale,
        "dismissed_candidates": dismissed,
        "affected_declarations": sorted(affected_declaration_ids),
    }
