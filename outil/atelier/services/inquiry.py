"""Recherche documentaire locale sur une entrée libre, sans brouillon prérempli.

Le résultat est une liste de candidats et d'exclusions, jamais une conclusion
politique automatique. Aucun réseau, modèle, shell ou création de revue.
"""
from __future__ import annotations

import re
import unicodedata
from django.core.exceptions import ValidationError
from atelier.models import Declaration, Scrutiny, VoteObservation, Mandate, ComparisonAct, FactQualification
from .catalogue import source_ref
from .canonical import sha256_digest


VERSION = "DV-LOCAL-INQUIRY-2"


def tokens(text):
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return set(re.findall(r"[a-z0-9]{3,}", normalized)) - {"les", "des", "une", "pour", "dans", "avec", "sur", "est", "pas", "que"}


def prepare_inquiry(*, query: str, actor_id: str = "", limit: int = 10,
                    scan_limit: int = 1000, declaration_offset: int = 0, scrutiny_offset: int = 0):
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 500 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValidationError("inquiry_input_invalid")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 1000 or any(type(v) is not int or not 0 <= v <= 1000000 for v in (declaration_offset, scrutiny_offset)):
        raise ValidationError("inquiry_scan_bounds_invalid")
    wanted = tokens(query)
    if not wanted:
        raise ValidationError("inquiry_terms_missing")
    declarations = Declaration.objects.select_related("speaker_person", "speaker_organization", "source_passage__document__source")
    if actor_id:
        from django.db.models import Q
        declarations = declarations.filter(Q(speaker_person__stable_id=actor_id) | Q(speaker_organization__stable_id=actor_id))
    # Le plafond de recherche est explicite, pas une fausse exhaustivité.
    candidates, exclusions = [], []
    total_declarations, total_scrutinies = declarations.count(), Scrutiny.objects.count()
    scan_votes = list(Scrutiny.objects.select_related("submitted_text", "source_document__source").order_by("occurred_on", "stable_id")[scrutiny_offset:scrutiny_offset+scan_limit])
    scanned_declarations = 0
    for declaration in declarations.order_by("stable_id")[declaration_offset:declaration_offset+scan_limit]:
        scanned_declarations += 1
        passage = declaration.source_passage
        linked_titles = list(ComparisonAct.objects.filter(comparison__declaration=declaration).values_list("scrutiny__official_title", flat=True)[:25])
        matched = sorted(wanted & tokens(" ".join([declaration.exact_quote, declaration.speaker_label, passage.context, *linked_titles])))
        if not matched:
            continue
        quote_supported = bool(declaration.exact_quote) and declaration.exact_quote in passage.exact_text
        if not quote_supported or passage.document.quarantined or passage.document.source.access_status not in {'active','qualified'} or passage.document.source.rights_status=='forbidden':
            exclusions.append({"declaration_id":declaration.stable_id, "reason":"quote_unbound_or_source_quarantined"})
            continue
        votes = []
        for scrutiny in scan_votes:
            terms = sorted(wanted & tokens(scrutiny.official_title))
            if not terms:
                continue
            qualification=FactQualification.objects.filter(scrutiny=scrutiny).order_by('-pk').first()
            if scrutiny.anomaly or scrutiny.source_document.quarantined or (qualification and qualification.state!='qualified') or scrutiny.source_document.source.access_status not in {'active','qualified'} or scrutiny.source_document.source.rights_status=='forbidden':
                exclusions.append({"scrutiny_id":scrutiny.stable_id, "reason":"scrutiny_unqualified_or_source_quarantined"})
                continue
            positions = list(VoteObservation.objects.filter(scrutiny=scrutiny, person_id=declaration.speaker_person_id).values_list("normalized_category", flat=True)) if declaration.speaker_person_id else []
            chronology = "unknown" if declaration.occurred_on is None else ("before_or_same_day" if declaration.occurred_on <= scrutiny.occurred_on else "after_vote")
            mandate = False
            if declaration.speaker_person_id:
                from django.db.models import Q
                mandate = Mandate.objects.filter(person_id=declaration.speaker_person_id, institution=scrutiny.institution,
                    starts_on__lte=scrutiny.occurred_on, source_document__isnull=False).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=scrutiny.occurred_on)).exists()
            reasons = ["Correspondance lexicale seulement : objet, portée et procédure à examiner."]
            if chronology == "after_vote":
                reasons.append("Déclaration postérieure : ne prouve pas une promesse antérieure.")
                exclusions.append({"declaration_id":declaration.stable_id, "scrutiny_id":scrutiny.stable_id,
                    "reason":"after_vote_not_evidence_of_prior_promise", "source_version":source_ref(passage.document)})
            if not positions:
                reasons.append("Aucune position nominative matérialisée : ne pas déduire une abstention.")
            if not mandate:
                reasons.append("Mandat à la date non établi par le corpus local.")
            votes.append({"scrutiny_id": scrutiny.stable_id, "matched_terms": terms,
                "official_text_url": scrutiny.submitted_text.official_url if scrutiny.submitted_text else None,
                "official_vote_url": scrutiny.official_vote_url, "source_version": source_ref(scrutiny.source_document),
                "positions_observed": sorted(set(positions)), "chronology": chronology,
                "mandate_verified_in_local_corpus": mandate, "included_as_comparable": False, "reservations": reasons})
            if len(votes) >= limit:
                break
        candidates.append({"declaration_id": declaration.stable_id, "speaker": declaration.speaker_label,
            "capacity": declaration.capacity, "matched_terms": matched, "exact_quote": declaration.exact_quote,
            "quote_status": passage.transcription_status, "source_version": source_ref(passage.document),
            "locator": passage.locator, "occurred_on": declaration.occurred_on.isoformat() if declaration.occurred_on else None,
            "vote_candidates": votes, "counterevidence_search_status": "not_exhaustive_local_candidates_only",
            "proposal": {"result": "inconclusive", "short_answer": "Pièces candidates réunies ; comparabilité et contre-éléments à vérifier.",
                "main_caveat": "Aucune conclusion politique ou revue humaine créée par cette recherche."}})
        if len(candidates) >= limit:
            break
    remaining_declarations = max(0, total_declarations - declaration_offset - scanned_declarations)
    remaining_scrutinies = max(0, total_scrutinies - scrutiny_offset - len(scan_votes))
    incomplete = bool(remaining_declarations or remaining_scrutinies or declaration_offset or scrutiny_offset)
    return {"processor_version": VERSION, "input_digest": sha256_digest({"query": query, "actor_id": actor_id, "limit": limit,
            "scan_limit":scan_limit, "declaration_offset":declaration_offset, "scrutiny_offset":scrutiny_offset}),
        "query": query, "actor_id": actor_id or None, "limit": limit, "scan_limit_per_table": scan_limit,
        "scan": {"declarations_total":total_declarations, "declarations_scanned":scanned_declarations,
            "declarations_offset":declaration_offset, "declarations_remaining":remaining_declarations,
            "next_declaration_offset":declaration_offset+scanned_declarations if remaining_declarations else None,
            "scrutinies_total":total_scrutinies, "scrutinies_scanned":len(scan_votes),
            "scrutinies_offset":scrutiny_offset, "scrutinies_remaining":remaining_scrutinies,
            "next_scrutiny_offset":scrutiny_offset+len(scan_votes) if remaining_scrutinies else None,
            "complete_materialized_scan":not incomplete, "complete_official_corpus":False},
        "absence_scope":"Absence éventuelle dans les déclarations normalisées parcourues seulement ; archives brutes et canaux extérieurs non parcourus intégralement.",
        "exclusions":exclusions,
        "coverage": "corpus local matérialisé uniquement", "candidates": candidates,
        "state": "candidates_for_review" if candidates else ("search_incomplete" if incomplete else "evidence_missing"), "provider_calls": 0, "cost_eur": "0",
        "human_review_created": False, "network_calls": 0}
