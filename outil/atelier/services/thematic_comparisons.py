"""Comparaisons factuelles parole publique → mesure → scrutin → vote.

Ce service ne découvre pas de relation politique par similarité lexicale. Il
restitue uniquement les relations documentaires dont la révision courante est
qualifiée et dont l'empreinte de contenu correspond encore à la matière
examinée, puis calcule une lecture déterministe et prudente du vote nominatif.
Il ne crée ni dossier éditorial, ni revue humaine, ni publication.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db.models import Q

from atelier.models import (
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    Measure,
    MeasureScrutinyLink,
    VoteObservation,
)
from atelier.services.thematic_integrity import (
    actors_payload,
    chronology_is_demonstrable,
    chronology_precision_reason,
    collective_vote_distribution,
    is_personal_comparable,
    link_matches_current_material,
    observation_source_usable,
    scrutiny_allows_nominative_attribution,
)


VERSION = "DV-THEMATIC-2"
MAX_RESULTS = 50
MAX_LINK_SCAN = 1000
MAX_SEARCH = 20


def _validated_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValidationError("thematic_limit_invalid")
    return limit


def _validated_offset(offset: int) -> int:
    if type(offset) is not int or offset < 0 or offset > 10_000:
        raise ValidationError("thematic_offset_invalid")
    return offset


def _optional_text(value: str | None, *, field: str, maximum: int = 120) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValidationError(f"thematic_{field}_invalid")
    return value.strip()


def _pagination(*, total: int, offset: int, limit: int, returned: int) -> dict:
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": returned,
        "has_more": offset + returned < total,
        "next_offset": offset + returned if offset + returned < total else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
    }


def _optional_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("thematic_period_invalid") from exc
    raise ValidationError("thematic_period_invalid")


def _validated_query(query: str) -> str:
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 300:
        raise ValidationError("thematic_query_invalid")
    return query.strip()


def _latest_by_pair(rows: Iterable, pair_fields: tuple[str, ...]) -> tuple[list, bool]:
    """Garde la révision la plus haute par relation, avec plafond explicite."""
    selected: dict[tuple, object] = {}
    scanned = 0
    for row in rows[: MAX_LINK_SCAN + 1]:
        scanned += 1
        if scanned > MAX_LINK_SCAN:
            return list(selected.values()), False
        key = tuple(getattr(row, field) for field in pair_fields)
        previous = selected.get(key)
        if previous is None or row.revision > previous.revision:
            selected[key] = row
    return list(selected.values()), True


def _link_exclusion_reason(link) -> str | None:
    if not link_matches_current_material(link):
        return "stale_fingerprint"
    passage = link.evidence
    document = passage.document
    if isinstance(link, DeclarationMeasureLink) and link.evidence_id != link.declaration.source_passage_id:
        return "evidence_not_source_passage"
    if isinstance(link, MeasureScrutinyLink) and link.evidence.document_id not in {
        link.scrutiny.source_document_id,
        link.scrutiny.submitted_text.source_document_id,
    }:
        return "evidence_not_scrutiny_or_text"
    if document.quarantined:
        return "document_quarantined"
    if document.source.access_status not in {"active", "qualified"}:
        return "source_not_admissible"
    if document.source.rights_status == "forbidden":
        return "source_forbidden"
    if not passage.public_url:
        return "missing_public_url"
    return None


def _evidence_is_usable(link) -> bool:
    return _link_exclusion_reason(link) is None


def _current_qualified_count(queryset, pair_fields: tuple[str, ...]) -> int:
    rows, _ = _latest_by_pair(queryset.order_by(*pair_fields, "-revision"), pair_fields)
    return sum(
        row.qualification_status == DocumentaryQualification.QUALIFIED and _evidence_is_usable(row)
        for row in rows
    )


def _source_payload(declaration: Declaration) -> dict:
    passage = declaration.source_passage
    document = passage.document
    return {
        "medium": declaration.medium,
        "medium_label": declaration.get_medium_display(),
        "source_role": declaration.source_role,
        "source_role_label": declaration.get_source_role_display(),
        "producer": document.source.producer,
        "family": document.source.family,
        "url": passage.public_url,
        "locator": passage.locator,
        "version_id": document.version_id,
        "sha256": document.sha256,
    }


_PRECISION_CAVEATS = {
    "unknown_state": "L'état de la date n'est pas connu ; aucune antériorité n'est inventée.",
    "missing_value": "Aucune valeur de date n'est associée à cet état.",
    "year": "Seule l'année est connue ; la chronologie journalière n'est pas démontrée.",
    "month": "Seul le mois est connu ; l'ordre des jours n'est pas démontré.",
    "interval": "La date est un intervalle ; aucun ordre certain n'est déduit.",
    "estimated": "La date est estimée ; elle ne produit pas une chronologie certaine.",
    "unknown": "La précision de date est inconnue.",
    "unknown_precision": "La précision de date n'est pas interprétable.",
}


def _chronology(declaration: Declaration, scrutiny) -> dict:
    reason = chronology_precision_reason(declaration)
    if not chronology_is_demonstrable(declaration):
        return {
            "code": "unknown",
            "label": "Chronologie non établie",
            "precision": reason,
            "caveat": _PRECISION_CAVEATS.get(reason, "La date ou sa précision ne permettent pas une chronologie démontrée."),
        }
    if declaration.occurred_on < scrutiny.occurred_on:
        return {
            "code": "before_vote",
            "label": "Parole antérieure au vote",
            "precision": reason,
            "caveat": "La chronologie journalière établit que la déclaration précède le scrutin.",
        }
    if declaration.occurred_on == scrutiny.occurred_on:
        return {
            "code": "same_day_order_unknown",
            "label": "Même jour, ordre non établi",
            "precision": reason,
            "caveat": "L'ordre précis entre la déclaration et le vote n'est pas établi.",
        }
    return {
        "code": "after_vote",
        "label": "Parole postérieure au vote",
        "precision": reason,
        "caveat": "Le vote est antérieur : cette relation ne prouve pas une promesse rompue.",
    }


def _vote_payload(declaration: Declaration, scrutiny) -> dict:
    if not is_personal_comparable(declaration):
        return {
            "state": "not_personal",
            "categories": [],
            "category": None,
            "label": "Portée collective ou indéterminée : aucun vote personnel attribué",
            "sources": [],
            "exclusions": [],
        }
    if not scrutiny_allows_nominative_attribution(scrutiny):
        return {
            "state": "non_nominative",
            "categories": [],
            "category": None,
            "label": "Scrutin sans détail nominatif utilisable",
            "sources": [],
            "exclusions": [
                {
                    "reason": "nominative_status_not_available",
                    "nominative_status": scrutiny.nominative_status,
                }
            ],
        }
    observations = list(
        VoteObservation.objects.filter(scrutiny=scrutiny, person_id=declaration.speaker_person_id)
        .select_related("source_document__source")
        .order_by("source_document__captured_at", "pk")
    )
    usable = []
    exclusions = []
    for item in observations:
        if observation_source_usable(item):
            usable.append(item)
        else:
            exclusions.append(
                {
                    "reason": "source_unusable",
                    "source_id": item.source_document.source.stable_id,
                    "version_id": item.source_document.version_id,
                    "quarantined": item.source_document.quarantined,
                    "access_status": item.source_document.source.access_status,
                    "rights_status": item.source_document.source.rights_status,
                }
            )
    categories = sorted({item.normalized_category for item in usable})
    sources = [
        {
            "source_id": item.source_document.source.stable_id,
            "version_id": item.source_document.version_id,
            "sha256": item.source_document.sha256,
        }
        for item in usable
    ]
    if not usable:
        state, category, label = "missing", None, "Aucun vote nominatif matérialisé utilisable"
    elif len(categories) != 1:
        state, category, label = "conflict", None, "Versions de source contradictoires"
    else:
        state, category = "observed", categories[0]
        label = dict(VoteObservation.Category.choices).get(category, category)
    return {
        "state": state,
        "categories": categories,
        "category": category,
        "label": label,
        "sources": sources,
        "exclusions": exclusions,
    }


def _conclusion(stance: str, vote_effect: str, vote: dict, chronology: dict) -> dict:
    if vote["state"] == "not_personal":
        return {
            "code": "not_comparable",
            "label": "Non comparable au niveau personnel",
            "explanation": "Une parole d'organisation ou de portée indéterminée ne permet pas d'attribuer le vote d'une personne.",
        }
    if vote["state"] == "non_nominative":
        return {
            "code": "not_comparable",
            "label": "Vote personnel non attribuable",
            "explanation": "Le scrutin n'offre pas de détail nominatif utilisable ; une ligne isolée n'est pas convertie en position personnelle.",
        }
    if vote["state"] == "missing":
        return {
            "code": "inconclusive",
            "label": "Vote personnel non établi",
            "explanation": "L'absence de vote matérialisé utilisable ne vaut ni opposition, ni abstention.",
        }
    if vote["state"] == "conflict":
        return {
            "code": "inconclusive",
            "label": "Sources de vote à réconcilier",
            "explanation": "Les versions conservées ne donnent pas une catégorie unique.",
        }
    category = vote["category"]
    if category not in {VoteObservation.Category.FOR, VoteObservation.Category.AGAINST}:
        return {
            "code": "not_comparable",
            "label": "Vote non directionnel",
            "explanation": "Abstention, non-participation ou état inconnu ne sont pas transformés en opposition.",
        }
    if stance not in {DeclarationMeasureLink.Stance.SUPPORT, DeclarationMeasureLink.Stance.OPPOSE}:
        return {
            "code": "not_comparable",
            "label": "Position publique trop conditionnelle",
            "explanation": "Le sens de la parole ne permet pas une comparaison binaire fidèle.",
        }
    if vote_effect not in {MeasureScrutinyLink.VoteEffect.ADVANCES, MeasureScrutinyLink.VoteEffect.BLOCKS}:
        return {
            "code": "not_comparable",
            "label": "Effet du scrutin trop composite",
            "explanation": "Le vote ne peut pas être ramené de façon fiable à faire avancer ou bloquer la mesure.",
        }

    advances = (vote_effect == MeasureScrutinyLink.VoteEffect.ADVANCES and category == VoteObservation.Category.FOR) or (
        vote_effect == MeasureScrutinyLink.VoteEffect.BLOCKS and category == VoteObservation.Category.AGAINST
    )
    aligned = advances if stance == DeclarationMeasureLink.Stance.SUPPORT else not advances
    if aligned:
        label = "Convergence documentée"
        explanation = "Le sens du vote observé va dans le même sens que la position publique qualifiée."
        code = "convergence"
    else:
        label = "Tension documentée"
        explanation = "Le sens du vote observé va à l'encontre de la position publique qualifiée."
        code = "tension"
    if chronology["code"] == "after_vote":
        explanation += " Il s'agit d'un vote antérieur, pas de la preuve d'une promesse rompue."
    elif chronology["code"] == "same_day_order_unknown":
        explanation += " L'ordre des événements dans la journée reste inconnu."
    elif chronology["code"] == "unknown":
        explanation += " La chronologie exacte reste inconnue."
    return {"code": code, "label": label, "explanation": explanation}


def _row(declaration_link: DeclarationMeasureLink, scrutiny_link: MeasureScrutinyLink) -> dict:
    declaration = declaration_link.declaration
    measure = declaration_link.measure
    scrutiny = scrutiny_link.scrutiny
    chronology = _chronology(declaration, scrutiny)
    vote = _vote_payload(declaration, scrutiny)
    actors = actors_payload(declaration)
    collective = None
    if not is_personal_comparable(declaration) and declaration.speaker_organization_id:
        collective = collective_vote_distribution(declaration.speaker_organization, scrutiny)
    return {
        "declaration": {
            "stable_id": declaration.stable_id,
            "speaker": declaration.speaker_label,
            "capacity": declaration.capacity,
            "position_scope": declaration.position_scope,
            "position_scope_label": declaration.get_position_scope_display(),
            "quote": declaration.exact_quote,
            "paraphrase": declaration.paraphrase,
            "occurred_on": declaration.occurred_on,
            "occurred_on_state": declaration.occurred_on_state,
            "date_precision": declaration.date_precision,
            "source": _source_payload(declaration),
            "content_fingerprint": declaration_link.content_fingerprint,
            "revision": declaration_link.revision,
        },
        "measure": {
            "stable_id": measure.stable_id,
            "title": measure.title,
            "plain_summary": measure.plain_summary,
            "concrete_effect": measure.concrete_effect,
            "mechanism": measure.mechanism,
            "official_text_url": measure.text_version.official_url,
            "position": declaration_link.stance,
            "position_label": declaration_link.get_stance_display(),
            "position_rationale": declaration_link.rationale,
        },
        "scrutiny": {
            "stable_id": scrutiny.stable_id,
            "title": scrutiny.official_title,
            "occurred_on": scrutiny.occurred_on,
            "institution": scrutiny.institution,
            "legislature": scrutiny.legislature,
            "nominative_status": scrutiny.nominative_status,
            "official_vote_url": scrutiny.official_vote_url,
            "vote_effect": scrutiny_link.vote_effect,
            "vote_effect_label": scrutiny_link.get_vote_effect_display(),
            "vote_effect_rationale": scrutiny_link.rationale,
            "content_fingerprint": scrutiny_link.content_fingerprint,
            "revision": scrutiny_link.revision,
        },
        "actors": actors,
        "vote": vote,
        "collective": collective,
        "chronology": chronology,
        "proofs": {
            "citation": {
                "kind": "declaration_source",
                "url": declaration.source_passage.public_url,
                "locator": declaration.source_passage.locator,
                "sha256": declaration.source_passage.document.sha256,
                "version_id": declaration.source_passage.document.version_id,
            },
            "text_version": {
                "kind": "official_text",
                "url": measure.text_version.official_url,
                "title": measure.text_version.official_title,
                "relation": measure.text_version.relation,
            },
            "scrutiny": {
                "kind": "official_vote",
                "url": scrutiny.official_vote_url,
                "title": scrutiny.official_title,
                "nominative_status": scrutiny.nominative_status,
            },
        },
        "conclusion": _conclusion(declaration_link.stance, scrutiny_link.vote_effect, vote, chronology),
    }


def _apply_row_filters(rows: list[dict], *, institution, legislature, nominative_status, conclusion, period_start, period_end) -> list[dict]:
    filtered = []
    for row in rows:
        scrutiny = row["scrutiny"]
        if institution and institution.lower() not in (scrutiny["institution"] or "").lower():
            continue
        if legislature and scrutiny.get("legislature") != legislature:
            continue
        if nominative_status and scrutiny["nominative_status"] != nominative_status:
            continue
        if conclusion and row["conclusion"]["code"] != conclusion:
            continue
        occurred = scrutiny["occurred_on"]
        if period_start and occurred < period_start:
            continue
        if period_end and occurred > period_end:
            continue
        filtered.append(row)
    return filtered


def _coverage_bundle(*, rows, limit, offset, scan_complete, stale_relations, relation_exclusions, filters) -> dict:
    matched = len(rows)
    page = rows[offset : offset + limit]
    stats = {
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "matched": matched,
        "scan_complete": scan_complete and offset + len(page) >= matched,
        "stale_relations": stale_relations,
        "relation_exclusions": relation_exclusions,
        "vote_exclusions": sum(len(row["vote"].get("exclusions") or []) for row in page),
        "denominator": "chaînes localement qualifiées à empreinte valide, après filtres",
        "filters_applied": {key: value for key, value in filters.items() if value not in (None, "")},
    }
    return {
        "rows": page,
        "coverage": (
            "Relations qualifiées à empreinte valide et votes nominatifs utilisables "
            "dans la base locale seulement."
        ),
        "coverage_stats": stats,
        "pagination": _pagination(total=matched, offset=offset, limit=limit, returned=len(page)),
    }


def _normalized_filters(**kwargs) -> dict:
    institution = kwargs.get("institution") or None
    legislature = kwargs.get("legislature") or None
    nominative_status = kwargs.get("nominative_status") or None
    conclusion = kwargs.get("conclusion") or None
    if institution is not None and (not isinstance(institution, str) or not 2 <= len(institution.strip()) <= 120):
        raise ValidationError("thematic_institution_invalid")
    if legislature is not None and (not isinstance(legislature, str) or not 1 <= len(legislature.strip()) <= 40):
        raise ValidationError("thematic_legislature_invalid")
    if nominative_status is not None and nominative_status not in {"available", "partial", "not_available"}:
        raise ValidationError("thematic_nominative_invalid")
    if conclusion is not None and conclusion not in {
        "convergence",
        "tension",
        "inconclusive",
        "not_comparable",
    }:
        raise ValidationError("thematic_conclusion_invalid")
    return {
        "institution": institution.strip() if isinstance(institution, str) else institution,
        "legislature": legislature.strip() if isinstance(legislature, str) else legislature,
        "nominative_status": nominative_status,
        "conclusion": conclusion,
        "period_start": _optional_date(kwargs.get("period_start")),
        "period_end": _optional_date(kwargs.get("period_end")),
    }


def search_declarations(
    *,
    query: str,
    limit: int = MAX_SEARCH,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    query = _validated_query(query)
    limit = _validated_limit(limit)
    if limit > MAX_SEARCH:
        limit = MAX_SEARCH
    offset = _validated_offset(offset)
    start = _optional_date(date_from)
    end = _optional_date(date_to)
    queryset = Declaration.objects.filter(
        Q(exact_quote__icontains=query)
        | Q(paraphrase__icontains=query)
        | Q(speaker_label__icontains=query)
        | Q(capacity__icontains=query)
    ).select_related("source_passage__document__source")
    if start:
        queryset = queryset.filter(occurred_on__gte=start)
    if end:
        queryset = queryset.filter(occurred_on__lte=end)
    total = queryset.count()
    rows = queryset.order_by("-occurred_on", "speaker_label", "stable_id")[offset : offset + limit]
    results = []
    for row in rows:
        results.append({
            "pk": row.pk,
            "stable_id": row.stable_id,
            "speaker": row.speaker_label,
            "capacity": row.capacity,
            "position_scope": row.position_scope,
            "position_scope_label": row.get_position_scope_display(),
            "quote": row.exact_quote,
            "occurred_on": row.occurred_on,
            "source": _source_payload(row),
            "qualified_relations": _current_qualified_count(
                row.measure_links.select_related(
                    "declaration",
                    "measure",
                    "evidence__document__source",
                ),
                ("declaration_id", "measure_id"),
            ),
        })
    return {
        "items": results,
        "pagination": _pagination(total=total, offset=offset, limit=limit, returned=len(results)),
        "filters": {"date_from": date_from or None, "date_to": date_to or None},
        "coverage": "Candidats locaux seulement ; aucune relation créée par la recherche.",
    }


def search_measures(
    *,
    query: str,
    limit: int = MAX_SEARCH,
    offset: int = 0,
    institution: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    query = _validated_query(query)
    limit = _validated_limit(limit)
    if limit > MAX_SEARCH:
        limit = MAX_SEARCH
    offset = _validated_offset(offset)
    institution = _optional_text(institution, field="institution")
    start = _optional_date(date_from)
    end = _optional_date(date_to)
    queryset = Measure.objects.filter(
        Q(title__icontains=query)
        | Q(plain_summary__icontains=query)
        | Q(concrete_effect__icontains=query)
        | Q(mechanism__icontains=query)
        | Q(text_version__official_title__icontains=query)
        | Q(text_version__dossier__official_title__icontains=query)
    ).select_related("text_version__dossier")
    if institution:
        queryset = queryset.filter(text_version__dossier__institution__icontains=institution)
    if start:
        queryset = queryset.filter(text_version__occurred_on__gte=start)
    if end:
        queryset = queryset.filter(text_version__occurred_on__lte=end)
    total = queryset.count()
    rows = queryset.order_by("text_version__occurred_on", "stable_id")[offset : offset + limit]
    results = []
    for row in rows:
        results.append({
            "pk": row.pk,
            "stable_id": row.stable_id,
            "title": row.title,
            "plain_summary": row.plain_summary,
            "concrete_effect": row.concrete_effect,
            "official_text_url": row.text_version.official_url,
            "dossier_title": row.text_version.dossier.official_title,
            "institution": row.text_version.dossier.institution,
            "qualified_declarations": _current_qualified_count(
                row.declaration_links.select_related(
                    "declaration",
                    "measure",
                    "evidence__document__source",
                ),
                ("declaration_id", "measure_id"),
            ),
            "qualified_scrutinies": _current_qualified_count(
                row.scrutiny_links.select_related(
                    "measure",
                    "scrutiny",
                    "evidence__document__source",
                ),
                ("measure_id", "scrutiny_id"),
            ),
        })
    return {
        "items": results,
        "pagination": _pagination(total=total, offset=offset, limit=limit, returned=len(results)),
        "filters": {"institution": institution, "date_from": date_from or None, "date_to": date_to or None},
        "coverage": "Candidats locaux seulement ; aucune relation créée par la recherche.",
    }


def compare_declaration(
    *,
    declaration_id: int,
    limit: int = MAX_RESULTS,
    offset: int = 0,
    institution: str | None = None,
    legislature: str | None = None,
    nominative_status: str | None = None,
    conclusion: str | None = None,
    period_start=None,
    period_end=None,
) -> dict:
    limit = _validated_limit(limit)
    offset = _validated_offset(offset)
    filters = _normalized_filters(
        institution=institution,
        legislature=legislature,
        nominative_status=nominative_status,
        conclusion=conclusion,
        period_start=period_start,
        period_end=period_end,
    )
    declaration = Declaration.objects.select_related(
        "source_passage__document__source",
        "speaker_person",
        "speaker_organization",
    ).get(pk=declaration_id)
    declaration_links, declaration_scan_complete = _latest_by_pair(
        DeclarationMeasureLink.objects.filter(declaration=declaration)
        .select_related(
            "declaration__speaker_person",
            "declaration__speaker_organization",
            "declaration__source_passage__document__source",
            "measure__text_version",
            "evidence__document__source",
        )
        .order_by("measure_id", "-revision"),
        ("declaration_id", "measure_id"),
    )
    stale_relations = 0
    relation_exclusions = []
    qualified_declaration_links = []
    for row in declaration_links:
        if row.qualification_status != DocumentaryQualification.QUALIFIED:
            continue
        reason = _link_exclusion_reason(row)
        if reason:
            if reason == "stale_fingerprint":
                stale_relations += 1
            relation_exclusions.append({"kind": "declaration_measure", "reason": reason, "revision": row.revision})
            continue
        qualified_declaration_links.append(row)
    rows = []
    scrutiny_scan_complete = True
    for declaration_link in qualified_declaration_links:
        scrutiny_links, complete = _latest_by_pair(
            MeasureScrutinyLink.objects.filter(measure=declaration_link.measure)
            .select_related(
                "measure__text_version",
                "scrutiny__source_document__source",
                "scrutiny__submitted_text",
                "evidence__document__source",
            )
            .order_by("scrutiny_id", "-revision"),
            ("measure_id", "scrutiny_id"),
        )
        scrutiny_scan_complete = scrutiny_scan_complete and complete
        for scrutiny_link in scrutiny_links:
            if scrutiny_link.qualification_status != DocumentaryQualification.QUALIFIED:
                continue
            reason = _link_exclusion_reason(scrutiny_link)
            if reason:
                if reason == "stale_fingerprint":
                    stale_relations += 1
                relation_exclusions.append({"kind": "measure_scrutiny", "reason": reason, "revision": scrutiny_link.revision})
                continue
            rows.append(_row(declaration_link, scrutiny_link))
    rows = _apply_row_filters(rows, **filters)
    bundle = _coverage_bundle(
        rows=rows,
        limit=limit,
        offset=offset,
        scan_complete=declaration_scan_complete and scrutiny_scan_complete,
        stale_relations=stale_relations,
        relation_exclusions=relation_exclusions,
        filters=filters,
    )
    return {
        "processor_version": VERSION,
        "direction": "speech_to_votes",
        "subject": {
            "stable_id": declaration.stable_id,
            "speaker": declaration.speaker_label,
            "position_scope": declaration.position_scope,
            "position_scope_label": declaration.get_position_scope_display(),
            "quote": declaration.exact_quote,
            "source": _source_payload(declaration),
            "actors": actors_payload(declaration),
        },
        "rows": bundle["rows"],
        "qualified_measure_count": len(qualified_declaration_links),
        "scan_complete": bundle["coverage_stats"]["scan_complete"],
        "pagination": bundle["pagination"],
        "filters": filters,
        "coverage": bundle["coverage"],
        "coverage_stats": bundle["coverage_stats"],
        "human_review_created": False,
        "publication_created": False,
        "network_calls": 0,
    }


def compare_measure(
    *,
    measure_id: int,
    limit: int = MAX_RESULTS,
    offset: int = 0,
    institution: str | None = None,
    legislature: str | None = None,
    nominative_status: str | None = None,
    conclusion: str | None = None,
    period_start=None,
    period_end=None,
) -> dict:
    limit = _validated_limit(limit)
    offset = _validated_offset(offset)
    filters = _normalized_filters(
        institution=institution,
        legislature=legislature,
        nominative_status=nominative_status,
        conclusion=conclusion,
        period_start=period_start,
        period_end=period_end,
    )
    measure = Measure.objects.select_related("text_version__dossier").get(pk=measure_id)
    declaration_links, declaration_scan_complete = _latest_by_pair(
        DeclarationMeasureLink.objects.filter(measure=measure)
        .select_related(
            "declaration__speaker_person",
            "declaration__speaker_organization",
            "declaration__source_passage__document__source",
            "measure",
            "evidence__document__source",
        )
        .order_by("declaration_id", "-revision"),
        ("declaration_id", "measure_id"),
    )
    scrutiny_links, scrutiny_scan_complete = _latest_by_pair(
        MeasureScrutinyLink.objects.filter(measure=measure)
        .select_related(
            "measure__text_version",
            "scrutiny__source_document__source",
            "scrutiny__submitted_text",
            "evidence__document__source",
        )
        .order_by("scrutiny_id", "-revision"),
        ("measure_id", "scrutiny_id"),
    )
    stale_relations = 0
    relation_exclusions = []
    usable_declaration_links = []
    for row in declaration_links:
        if row.qualification_status != DocumentaryQualification.QUALIFIED:
            continue
        reason = _link_exclusion_reason(row)
        if reason:
            if reason == "stale_fingerprint":
                stale_relations += 1
            relation_exclusions.append({"kind": "declaration_measure", "reason": reason, "revision": row.revision})
            continue
        usable_declaration_links.append(row)
    usable_scrutiny_links = []
    for row in scrutiny_links:
        if row.qualification_status != DocumentaryQualification.QUALIFIED:
            continue
        reason = _link_exclusion_reason(row)
        if reason:
            if reason == "stale_fingerprint":
                stale_relations += 1
            relation_exclusions.append({"kind": "measure_scrutiny", "reason": reason, "revision": row.revision})
            continue
        usable_scrutiny_links.append(row)
    rows = []
    for declaration_link in usable_declaration_links:
        for scrutiny_link in usable_scrutiny_links:
            rows.append(_row(declaration_link, scrutiny_link))
    rows = _apply_row_filters(rows, **filters)
    bundle = _coverage_bundle(
        rows=rows,
        limit=limit,
        offset=offset,
        scan_complete=declaration_scan_complete and scrutiny_scan_complete,
        stale_relations=stale_relations,
        relation_exclusions=relation_exclusions,
        filters=filters,
    )
    return {
        "processor_version": VERSION,
        "direction": "law_to_public_positions",
        "subject": {
            "stable_id": measure.stable_id,
            "title": measure.title,
            "plain_summary": measure.plain_summary,
            "concrete_effect": measure.concrete_effect,
            "official_text_url": measure.text_version.official_url,
            "dossier_title": measure.text_version.dossier.official_title,
        },
        "rows": bundle["rows"],
        "qualified_declaration_count": len(usable_declaration_links),
        "qualified_scrutiny_count": len(usable_scrutiny_links),
        "scan_complete": bundle["coverage_stats"]["scan_complete"],
        "pagination": bundle["pagination"],
        "filters": filters,
        "coverage": bundle["coverage"],
        "coverage_stats": bundle["coverage_stats"],
        "human_review_created": False,
        "publication_created": False,
        "network_calls": 0,
    }
