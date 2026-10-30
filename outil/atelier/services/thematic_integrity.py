"""Intégrité documentaire du comparateur thématique (SUITE-005 / C01–C04).

Les relations qualifiées sont liées à l'empreinte de la matière examinée.
Une modification substantielle sans nouvelle qualification n'autorise plus de
conclusion directionnelle. Ce module ne crée ni revue humaine ni publication.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Q

from atelier.models import (
    Affiliation,
    DataState,
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    Measure,
    MeasureScrutinyLink,
    Scrutiny,
    VoteObservation,
)


FINGERPRINT_VERSION = "DV-THEMATIC-FP-4"
LEGACY_FINGERPRINT_VERSIONS = ("DV-THEMATIC-FP-2", "DV-THEMATIC-FP-3")
# Champs d'affichage ou d'audit qui ne font pas partie de la matière qualifiée.
NON_INVALIDATING_DECLARATION_FIELDS = (
    "speaker_label",
    "verification_status",
    "published_on",
    "amount",
    "amount_unit",
)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def declaration_measure_material(
    *,
    declaration: Declaration,
    measure: Measure,
    evidence,
    stance: str,
) -> dict[str, Any]:
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "kind": "declaration_measure",
        "declaration_id": declaration.pk,
        "measure_id": measure.pk,
        "stance": stance,
        "exact_quote": declaration.exact_quote,
        "paraphrase": declaration.paraphrase,
        "capacity": declaration.capacity,
        "quantifier": declaration.quantifier,
        "conditions": declaration.conditions,
        "speaker_person_id": declaration.speaker_person_id,
        "speaker_organization_id": declaration.speaker_organization_id,
        "position_scope": declaration.position_scope,
        # C01/C04 : la date qualifiée (valeur, état, précision) appartient à la matière.
        "occurred_on": declaration.occurred_on.isoformat() if declaration.occurred_on else None,
        "occurred_interval_end": (
            declaration.occurred_interval_end.isoformat() if declaration.occurred_interval_end else None
        ),
        "occurred_on_state": declaration.occurred_on_state,
        "date_precision": declaration.date_precision,
        "measure_title": measure.title,
        "measure_locator": measure.locator,
        "plain_summary": measure.plain_summary,
        "concrete_effect": measure.concrete_effect,
        "mechanism": measure.mechanism,
        "evidence_id": getattr(evidence, "pk", None),
        "evidence_text": getattr(evidence, "exact_text", ""),
        "evidence_locator": getattr(evidence, "locator", ""),
        "evidence_document_sha256": getattr(getattr(evidence, "document", None), "sha256", ""),
        "evidence_document_version": getattr(getattr(evidence, "document", None), "version_id", ""),
    }


def measure_scrutiny_material(
    *,
    measure: Measure,
    scrutiny: Scrutiny,
    evidence,
    vote_effect: str,
) -> dict[str, Any]:
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "kind": "measure_scrutiny",
        "measure_id": measure.pk,
        "scrutiny_id": scrutiny.pk,
        "vote_effect": vote_effect,
        "measure_title": measure.title,
        "measure_locator": measure.locator,
        "plain_summary": measure.plain_summary,
        "concrete_effect": measure.concrete_effect,
        "mechanism": measure.mechanism,
        "scrutiny_title": scrutiny.official_title,
        "scrutiny_occurred_on": scrutiny.occurred_on.isoformat() if scrutiny.occurred_on else None,
        "nominative_status": scrutiny.nominative_status,
        "procedure": scrutiny.procedure,
        "evidence_id": getattr(evidence, "pk", None),
        "evidence_text": getattr(evidence, "exact_text", ""),
        "evidence_locator": getattr(evidence, "locator", ""),
        "evidence_document_sha256": getattr(getattr(evidence, "document", None), "sha256", ""),
        "evidence_document_version": getattr(getattr(evidence, "document", None), "version_id", ""),
    }


def fingerprint_declaration_measure_link(link: DeclarationMeasureLink) -> str:
    return _digest(
        declaration_measure_material(
            declaration=link.declaration,
            measure=link.measure,
            evidence=link.evidence,
            stance=link.stance,
        )
    )


def fingerprint_measure_scrutiny_link(link: MeasureScrutinyLink) -> str:
    return _digest(
        measure_scrutiny_material(
            measure=link.measure,
            scrutiny=link.scrutiny,
            evidence=link.evidence,
            vote_effect=link.vote_effect,
        )
    )


def link_matches_current_material(link) -> bool:
    if not getattr(link, "content_fingerprint", ""):
        return False
    if isinstance(link, DeclarationMeasureLink):
        return link.content_fingerprint == fingerprint_declaration_measure_link(link)
    if isinstance(link, MeasureScrutinyLink):
        return link.content_fingerprint == fingerprint_measure_scrutiny_link(link)
    return False


def is_personal_comparable(declaration: Declaration) -> bool:
    """Une porte-parole avec organisation n'est jamais personnellement attribuée par défaut."""
    if declaration.position_scope == Declaration.PositionScope.COLLECTIVE:
        return False
    if declaration.position_scope == Declaration.PositionScope.PERSONAL:
        return declaration.speaker_person_id is not None
    if declaration.speaker_organization_id:
        return False
    return declaration.speaker_person_id is not None


def chronology_is_demonstrable(declaration: Declaration) -> bool:
    return chronology_precision_reason(declaration) == "day"


def chronology_precision_reason(declaration: Declaration) -> str:
    """Valeur, état et précision lus conjointement (C04)."""
    if declaration.occurred_on_state != DataState.KNOWN:
        return "unknown_state"
    if declaration.occurred_on is None:
        return "missing_value"
    if declaration.date_precision == Declaration.DatePrecision.DAY:
        return "day"
    if declaration.date_precision in dict(Declaration.DatePrecision.choices):
        return declaration.date_precision
    return "unknown_precision"


def declaration_value_for_precision(declaration: Declaration) -> str | None:
    if declaration.occurred_on is None:
        return None
    day = declaration.occurred_on
    precision = declaration.date_precision
    if precision == Declaration.DatePrecision.DAY:
        return day.isoformat()
    if precision == Declaration.DatePrecision.MONTH:
        return day.strftime("%Y-%m")
    if precision == Declaration.DatePrecision.YEAR:
        return day.strftime("%Y")
    if precision in {
        Declaration.DatePrecision.INTERVAL,
        Declaration.DatePrecision.ESTIMATED,
        Declaration.DatePrecision.UNKNOWN,
    }:
        return day.isoformat()
    return day.isoformat()


def qualified_declaration_payload(declaration: Declaration) -> dict[str, Any]:
    """Représentation typée C01 : valeur, état, précision et bornes cohérents."""
    precision = str(declaration.date_precision)
    state = str(declaration.occurred_on_state)
    value = declaration_value_for_precision(declaration) if state == DataState.KNOWN else None
    interval_start = None
    interval_end = None
    if precision == Declaration.DatePrecision.INTERVAL and state == DataState.KNOWN and declaration.occurred_on:
        if (
            declaration.occurred_interval_end
            and declaration.occurred_interval_end < declaration.occurred_on
        ):
            raise ValidationError("qualified_declaration_interval_inverted")
        interval_start = declaration.occurred_on.isoformat()
        if declaration.occurred_interval_end:
            interval_end = declaration.occurred_interval_end.isoformat()
    return {
        "state": state,
        "precision": precision,
        "value": value,
        "interval_start": interval_start,
        "interval_end": interval_end,
    }


def compared_actor_scope(declaration: Declaration) -> str:
    if declaration.position_scope == Declaration.PositionScope.COLLECTIVE:
        return "collective"
    if declaration.position_scope == Declaration.PositionScope.PERSONAL:
        return "personal"
    return "indeterminate"


def actors_payload_for_public_contract(declaration: Declaration) -> dict[str, Any]:
    """Projection contrat public (person_id / organization_id) depuis la qualification interne."""
    internal = actors_payload(declaration)
    compared = internal["compared_actor"]
    contract_compared = {
        "person_id": compared["id"] if compared.get("kind") == "person" else None,
        "organization_id": compared["id"] if compared.get("kind") == "organization" else None,
        "label": compared.get("label"),
    }
    represented = internal.get("represented_entity") or {}
    return {
        "speaker": internal["speaker"],
        "represented_entity": {"organization_id": represented.get("organization_id")},
        "compared_actor": contract_compared,
        "scope": compared_actor_scope(declaration),
    }


def collective_distribution_contract(collective: dict[str, Any] | None) -> dict[str, Any] | None:
    if collective is None:
        return None
    from atelier.models import Person

    state_map = {
        "group_distribution": "available",
        "no_dated_affiliation": "partial",
        "non_nominative": "unavailable",
        "unavailable": "unavailable",
    }
    person_pks = {
        voice["person_id"]
        for voice in collective.get("voices", [])
        if voice.get("person_id") is not None
    }
    person_pks.update(
        item["person_id"]
        for item in collective.get("exclusions", [])
        if item.get("person_id") is not None
    )
    stable_by_pk = {
        row.pk: row.stable_id for row in Person.objects.filter(pk__in=person_pks).only("pk", "stable_id")
    }
    included = {voice.get("person_id") for voice in collective.get("voices", []) if voice.get("person_id") is not None}
    excluded = {item.get("person_id") for item in collective.get("exclusions", []) if item.get("person_id") is not None}
    unresolved = excluded - included
    state = state_map.get(collective.get("state"), "unavailable")
    # Un membre daté sans position exploitable ne devient ni abstention ni vote
    # connu. Un doublon documentaire exclu n'efface pas une autre voix utilisable.
    unknown_count = collective.get("unknown_member_count")
    if collective.get("state") == "group_distribution" and unresolved:
        state = "partial"
        unknown_count = len(unresolved)
    return {
        "state": state,
        "rule_id": collective.get("rule_id"),
        "rule_version": collective.get("rule_version"),
        "as_of": collective.get("as_of"),
        "included_member_ids": [
            stable_by_pk[voice["person_id"]]
            for voice in collective.get("voices", [])
            if voice.get("person_id") in stable_by_pk
        ],
        "excluded_member_ids": [
            stable_by_pk[item["person_id"]]
            for item in collective.get("exclusions", [])
            if item.get("person_id") in stable_by_pk
        ],
        "unknown_member_count": unknown_count,
    }


def primary_scrutiny_for_comparison(comparison) -> Scrutiny | None:
    act = (
        comparison.acts.filter(included=True)
        .select_related("scrutiny")
        .order_by("scrutiny__stable_id")
        .first()
    )
    if act:
        return act.scrutiny
    act = comparison.acts.select_related("scrutiny").order_by("scrutiny__stable_id").first()
    return act.scrutiny if act else None


def latest_measure_scrutiny_links(declaration: Declaration, scrutiny: Scrutiny) -> list[MeasureScrutinyLink]:
    measure_ids = list(
        DeclarationMeasureLink.objects.filter(declaration=declaration).values_list("measure_id", flat=True).distinct()
    )
    if not measure_ids:
        return []
    selected: dict[int, MeasureScrutinyLink] = {}
    rows = (
        MeasureScrutinyLink.objects.filter(measure_id__in=measure_ids, scrutiny=scrutiny)
        .select_related("measure", "scrutiny", "evidence__document")
        .order_by("measure_id", "-revision")
    )
    for row in rows:
        selected.setdefault(row.measure_id, row)
    return list(selected.values())


def qualification_material_for_comparison(comparison) -> dict[str, Any]:
    """Matière C01–C04 figée pour empreintes et revue, sans règle politique inventée."""
    declaration = comparison.declaration
    scrutiny = primary_scrutiny_for_comparison(comparison)
    declaration_measure_links = []
    for link in latest_declaration_measure_links(declaration):
        evidence = link.evidence
        declaration_measure_links.append(
            {
                "measure_id": link.measure.stable_id,
                "revision": link.revision,
                "stance": link.stance,
                "qualification_status": link.qualification_status,
                "content_fingerprint": link.content_fingerprint,
                "evidence": {
                    "document_version": evidence.document.version_id,
                    "sha256": evidence.document.sha256,
                    "locator": evidence.locator,
                    "exact_text": evidence.exact_text,
                },
            }
        )
    measure_scrutiny_links = []
    if scrutiny:
        for link in latest_measure_scrutiny_links(declaration, scrutiny):
            evidence = link.evidence
            measure_scrutiny_links.append(
                {
                    "measure_id": link.measure.stable_id,
                    "scrutiny_id": link.scrutiny.stable_id,
                    "revision": link.revision,
                    "vote_effect": link.vote_effect,
                    "qualification_status": link.qualification_status,
                    "content_fingerprint": link.content_fingerprint,
                    "evidence": {
                        "document_version": evidence.document.version_id,
                        "sha256": evidence.document.sha256,
                        "locator": evidence.locator,
                        "exact_text": evidence.exact_text,
                    },
                }
            )
    collective = None
    if scrutiny and not is_personal_comparable(declaration) and declaration.speaker_organization_id:
        collective = collective_vote_distribution(declaration.speaker_organization, scrutiny)
    passage = declaration.source_passage
    return {
        "declaration_id": declaration.stable_id,
        "qualified_declaration": qualified_declaration_payload(declaration),
        "actors": actors_payload_for_public_contract(declaration),
        "declaration_measure_links": declaration_measure_links,
        "measure_scrutiny_links": measure_scrutiny_links,
        "declaration_source": {
            "public_url": passage.public_url,
            "locator": passage.locator,
            "document_version": passage.document.version_id,
            "sha256": passage.document.sha256,
        },
        "scrutiny_id": scrutiny.stable_id if scrutiny else None,
        "collective_vote_distribution": collective,
    }


def actors_payload(declaration: Declaration) -> dict[str, Any]:
    """Orateur, entité représentée et acteur comparé restent distincts (C03)."""
    speaker = {
        "person_id": declaration.speaker_person.stable_id if declaration.speaker_person_id else None,
        "label": declaration.speaker_label,
        "capacity": declaration.capacity,
    }
    represented = None
    if declaration.speaker_organization_id:
        represented = {
            "organization_id": declaration.speaker_organization.stable_id,
            "name": declaration.speaker_organization.name,
            "kind": declaration.speaker_organization.kind,
        }
    if is_personal_comparable(declaration):
        compared = {
            "kind": "person",
            "id": declaration.speaker_person.stable_id,
            "label": declaration.speaker_person.display_name,
        }
    elif (
        declaration.position_scope == Declaration.PositionScope.COLLECTIVE
        and declaration.speaker_organization_id
    ):
        compared = {
            "kind": "organization",
            "id": declaration.speaker_organization.stable_id,
            "label": declaration.speaker_organization.name,
        }
    else:
        compared = {"kind": "undetermined", "id": None, "label": None}
    return {
        "speaker": speaker,
        "represented_entity": represented,
        "compared_actor": compared,
        "position_scope": declaration.position_scope,
    }


def collective_vote_distribution(organization, scrutiny: Scrutiny) -> dict[str, Any]:
    """Groupe → affiliations datées → une voix par personne ; jamais une position personnelle."""
    reserve = (
        "Répartition de groupe à la date du scrutin ; ce n'est pas la position "
        "personnelle du porte-parole."
    )
    if organization is None:
        return {
            "state": "unavailable",
            "affiliations": 0,
            "usable_observations": 0,
            "usable_voices": 0,
            "distribution": {},
            "voices": [],
            "exclusions": [],
            "reserve": reserve,
        }
    if not scrutiny_allows_nominative_attribution(scrutiny):
        return {
            "state": "non_nominative",
            "affiliations": 0,
            "usable_observations": 0,
            "usable_voices": 0,
            "distribution": {},
            "voices": [],
            "exclusions": [
                {
                    "reason": "nominative_status_not_available",
                    "nominative_status": scrutiny.nominative_status,
                }
            ],
            "reserve": reserve,
        }
    affiliates = Affiliation.objects.filter(
        organization=organization,
        starts_on__lte=scrutiny.occurred_on,
    ).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=scrutiny.occurred_on))
    person_ids = list(affiliates.values_list("person_id", flat=True).distinct())
    observations = list(
        VoteObservation.objects.filter(scrutiny=scrutiny, person_id__in=person_ids)
        .select_related("source_document__source")
        .order_by("person_id", "source_document__captured_at", "pk")
    )
    by_person: dict[int, list] = {}
    for item in observations:
        by_person.setdefault(item.person_id, []).append(item)

    distribution: dict[str, int] = {}
    voices: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    usable_observation_count = 0

    for person_id in person_ids:
        items = by_person.get(person_id, [])
        usable = []
        for item in items:
            if observation_source_usable(item):
                usable.append(item)
            else:
                exclusions.append(
                    {
                        "reason": "source_unusable",
                        "person_id": person_id,
                        "source_id": item.source_document.source.stable_id,
                        "version_id": item.source_document.version_id,
                        "quarantined": item.source_document.quarantined,
                        "access_status": item.source_document.source.access_status,
                        "rights_status": item.source_document.source.rights_status,
                    }
                )
        if not usable:
            exclusions.append(
                {
                    "reason": "affiliation_without_usable_vote",
                    "person_id": person_id,
                    "count": 1,
                }
            )
            continue
        usable_observation_count += len(usable)
        categories = sorted({item.normalized_category for item in usable})
        sources = [
            {
                "source_id": item.source_document.source.stable_id,
                "version_id": item.source_document.version_id,
                "sha256": item.source_document.sha256,
                "category": item.normalized_category,
            }
            for item in usable
        ]
        if len(categories) != 1:
            exclusions.append(
                {
                    "reason": "person_vote_conflict",
                    "person_id": person_id,
                    "categories": categories,
                    "sources": sources,
                }
            )
            continue
        category = categories[0]
        distribution[category] = distribution.get(category, 0) + 1
        voices.append(
            {
                "person_id": person_id,
                "category": category,
                "sources": sources,
                "observation_count": len(usable),
            }
        )

    as_of = scrutiny.occurred_on.isoformat() if scrutiny.occurred_on else None
    return {
        "state": "group_distribution" if person_ids else "no_dated_affiliation",
        "rule_id": "COLLECTIVE_AFFILIATION_DATED_V1",
        "rule_version": "racc001-1",
        "as_of": as_of,
        "affiliations": len(person_ids),
        "usable_observations": usable_observation_count,
        "usable_voices": len(voices),
        "distribution": distribution,
        "voices": voices,
        "exclusions": exclusions,
        "reserve": reserve,
    }


def latest_declaration_measure_links(declaration: Declaration) -> list[DeclarationMeasureLink]:
    selected: dict[int, DeclarationMeasureLink] = {}
    rows = DeclarationMeasureLink.objects.filter(declaration=declaration).select_related(
        "declaration__source_passage__document",
        "measure",
        "evidence__document",
    ).order_by("measure_id", "-revision")
    for row in rows:
        selected.setdefault(row.measure_id, row)
    return list(selected.values())


def thematic_preparation_snapshot(declaration: Declaration) -> dict[str, Any]:
    """Contrôle C01 pendant la préparation d'un dossier : l'empreinte, pas une conclusion."""
    stale = []
    current = []
    for link in latest_declaration_measure_links(declaration):
        payload = {
            "measure_id": link.measure.stable_id,
            "revision": link.revision,
            "qualification_status": link.qualification_status,
            "fingerprint": link.content_fingerprint,
            "matches_current_material": link_matches_current_material(link),
        }
        if link.qualification_status == DocumentaryQualification.QUALIFIED and not payload["matches_current_material"]:
            stale.append(payload)
        else:
            current.append(payload)
    return {
        "declaration_id": declaration.stable_id,
        "actors": actors_payload(declaration),
        "qualified_links": current,
        "stale_qualified_links": stale,
        "comparability_established": False,
        "human_review_created": False,
    }


def observation_source_usable(observation: VoteObservation) -> bool:
    document = observation.source_document
    source = document.source
    return (
        not document.quarantined
        and source.access_status in {"active", "qualified"}
        and source.rights_status != "forbidden"
    )


def scrutiny_allows_nominative_attribution(scrutiny: Scrutiny) -> bool:
    return scrutiny.nominative_status in {
        Scrutiny.NominativeStatus.AVAILABLE,
        Scrutiny.NominativeStatus.PARTIAL,
    }
