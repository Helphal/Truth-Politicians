"""Instantanés factuels exportables reliés aux objets normalisés et à leurs sources.

Les relations absentes restent absentes : aucune affiliation ni aucun vote individuel
n'est déduit d'un libellé de groupe. Ce module ne certifie pas les documents eux-mêmes.
"""
from __future__ import annotations

from copy import deepcopy
from django.core.exceptions import ValidationError
from atelier.models import Affiliation, Mandate, Person, Organization, Scrutiny, VoteObservation
from .canonical import sha256_digest


def source_ref(document):
    if document is None:
        raise ValidationError("normalized_source_missing")
    return {"source_id": document.source.stable_id, "version_id": document.version_id,
            "sha256": document.sha256, "public_url": document.source.document_url}


def state_count(value):
    return {"state": "known", "value": value} if value is not None else {
        "state": "unknown", "value": None, "reason": "Non établi par la pièce normalisée."}


def official_fields(scrutiny):
    if hasattr(scrutiny,'identity_correction'):
        raise ValidationError('retired_scrutiny_identity')
    from atelier.models import FactQualification
    qualification=FactQualification.objects.filter(scrutiny=scrutiny).order_by('-created_at','-pk').first()
    if qualification and qualification.state!='qualified':
        raise ValidationError('exact_act_qualification_pending')
    text = scrutiny.submitted_text
    if text is None or text.relation != "submitted" or scrutiny.anomaly:
        raise ValidationError("scrutiny_unqualified_or_submitted_text_missing")
    return {
        # La clé interne du Sénat utilise sa session pour éviter les collisions
        # de numéros. Ce namespace n'est pas une législature documentée.
        "institution": scrutiny.institution, "legislature": (
            "Non renseignée dans cette fiche ; voir la session"
            if scrutiny.institution.casefold() in {"sénat", "senat"} else scrutiny.legislature),
        "session": scrutiny.session, "official_title": scrutiny.official_title,
        "scrutin_id": scrutiny.stable_id, "occurred_on": scrutiny.occurred_on.isoformat(),
        "date_precision": scrutiny.date_precision, "procedure": scrutiny.procedure,
        "stage": text.stage, "result_status": scrutiny.result_status,
        "official_text_url": text.official_url, "official_vote_url": scrutiny.official_vote_url,
        "text_reference_role": "submitted", "nominative_status": str(scrutiny.nominative_status),
        "counts": {key: state_count(getattr(scrutiny, attr)) for key, attr in (
            ("registered", "registered"), ("voters", "voters"), ("expressed", "expressed"),
            ("for", "count_for"), ("against", "count_against"), ("abstention", "count_abstention"))},
    }


def _date(value):
    return value.isoformat() if value else None


def _build_legacy_catalogue(scrutiny_ids, *, extra_person_ids=(), extra_organization_ids=(), limit=250,
                            collective_group=None, observed_members_only=False, binding_refs=()):
    identifiers = sorted(set(scrutiny_ids))
    if not identifiers or len(identifiers) > 25:
        raise ValidationError("catalogue_scope_missing_or_excessive")
    scrutinies = list(Scrutiny.objects.filter(stable_id__in=identifiers).select_related(
        "source_document__source", "submitted_text__source_document__source", "submitted_text__dossier").order_by("stable_id"))
    if len(scrutinies) != len(identifiers):
        raise ValidationError("normalized_scrutiny_missing")
    if binding_refs:
        from .catalogue_v2 import select_scrutiny_text_bindings
        selected = select_scrutiny_text_bindings(identifiers, binding_refs)
        for scrutiny in scrutinies:
            binding = selected.get(scrutiny.stable_id)
            if binding:
                # This is a transient projection of loaded objects. The
                # historical FK, procedure and source row are never saved.
                scrutiny.submitted_text = binding.submitted_text
                scrutiny.source_document = binding.source_document
                scrutiny.procedure = binding.procedure
                scrutiny.official_vote_url = binding.source_document.source.document_url
    observation_query = VoteObservation.objects.filter(scrutiny__in=scrutinies)
    selection = None
    if collective_group is not None:
        from django.db.models import Q
        if len(scrutinies) != 1 or collective_group.kind != 'parliamentary_group':
            raise ValidationError('collective_catalogue_requires_one_act_and_group')
        as_of = scrutinies[0].occurred_on
        members = Affiliation.objects.filter(organization=collective_group, starts_on__lte=as_of).filter(
            Q(ends_on__isnull=True) | Q(ends_on__gte=as_of))
        if observed_members_only:
            from .provenance import OBSERVATION_STATUSES
            # Open-ended AMO metadata does not attest continued membership.
            members = members.filter(Q(status__in=OBSERVATION_STATUSES, observed_on=as_of) |
                (~Q(status__in=OBSERVATION_STATUSES) & Q(ends_on__isnull=False, ends_on__gte=as_of)))
        if members.filter(source_document__isnull=True).exists():
            raise ValidationError('collective_catalogue_unsourced_membership')
        member_ids = set(members.values_list('person_id', flat=True))
        if observed_members_only:
            from atelier.models import AffiliationObservation
            member_ids |= set(AffiliationObservation.objects.filter(organization=collective_group,
                observed_on=as_of).values_list('person_id', flat=True))
        if not member_ids or len(member_ids) > limit:
            raise ValidationError('collective_catalogue_members_missing_or_excessive')
        # No client-supplied list, no truncation: retain every dated member.
        observation_query = observation_query.filter(person_id__in=member_ids)
        extra_person_ids = set(extra_person_ids) | member_ids
        selection = {'contract': 'dv.catalogue-selection/1', 'kind': 'dated_collective_members',
            'group_id': collective_group.stable_id, 'as_of': as_of.isoformat(),
            'scope_person_ids': sorted(Person.objects.filter(pk__in=member_ids).values_list('stable_id', flat=True))}
        if observed_members_only:
            selection.update(contract='dv.catalogue-selection/2', kind='documented_at_event',
                limit='An observation at this event or a closed institutional interval; no current membership inference.')
    observations = list(observation_query.select_related(
        "scrutiny", "person", "affiliation_at_event", "mandate", "source_document__source").order_by("scrutiny__stable_id", "person__stable_id", "source_document_id"))
    if len(observations) > limit:
        raise ValidationError("catalogue_batch_too_large_split_scope")
    for observation in observations:
        observation.clean()
    people_ids = {item.person_id for item in observations} | set(extra_person_ids)
    org_ids = {item.affiliation_at_event_id for item in observations if item.affiliation_at_event_id} | set(extra_organization_ids)
    first, last = min(s.occurred_on for s in scrutinies), max(s.occurred_on for s in scrutinies)
    from django.db.models import Q
    affiliation_query = Affiliation.objects.filter(person_id__in=people_ids, starts_on__lte=last).filter(
        Q(ends_on__isnull=True) | Q(ends_on__gte=first))
    if collective_group is not None:
        affiliation_query = affiliation_query.filter(organization=collective_group)
    affiliations = list(affiliation_query.select_related("person", "organization", "source_document__source").order_by("person__stable_id", "starts_on", "organization__stable_id"))
    mandates = list(Mandate.objects.filter(person_id__in=people_ids, starts_on__lte=last).filter(
        Q(ends_on__isnull=True) | Q(ends_on__gte=first)).select_related("person", "source_document__source").order_by("stable_id"))
    org_ids |= {item.organization_id for item in affiliations}
    docs = {}
    def pin(document):
        ref = source_ref(document)
        docs[(ref["source_id"], ref["version_id"])] = ref
        return ref
    texts, legislative = {}, {}
    votes = []
    for scrutiny in scrutinies:
        text = scrutiny.submitted_text
        fields = official_fields(scrutiny)
        fields["source_version"] = pin(scrutiny.source_document)
        votes.append(fields)
        texts[text.stable_id] = {
            "id": text.stable_id, "dossier_id": text.dossier.stable_id,
            "kind": text.kind, "official_title": text.official_title,
            "version_label": text.version_label, "stage": text.stage,
            "relation": text.relation, "occurred_on": _date(text.occurred_on),
            "official_url": text.official_url, "source_version": pin(text.source_document),
        }
        dossier = text.dossier
        legislative[dossier.stable_id] = {"id": dossier.stable_id, "institution": dossier.institution,
            "official_title": dossier.official_title, "official_url": dossier.official_url, "status": dossier.status}
    positions = [{"scrutiny_id": v.scrutiny.stable_id, "person_id": v.person.stable_id,
        "mandate_id": v.mandate.stable_id if v.mandate else None,
        "affiliation_at_event_id": v.affiliation_at_event.stable_id if v.affiliation_at_event else None,
        "source_category": v.source_category, "normalized_category": v.normalized_category,
        "mapping_rule": v.mapping_rule, "source_version": pin(v.source_document)} for v in observations]
    people = [{"id": p.stable_id, "display_name": p.display_name, "institutional_ids": p.institutional_ids,
               "identity_status": p.identity_status} for p in Person.objects.filter(pk__in=people_ids).order_by("stable_id")]
    organizations = [{"id": o.stable_id, "name": o.name, "kind": o.kind,
        "valid_from": _date(o.valid_from), "valid_to": _date(o.valid_to)} for o in Organization.objects.filter(pk__in=org_ids).order_by("stable_id")]
    affiliate_rows = [{"person_id": a.person.stable_id, "organization_id": a.organization.stable_id,
        "status": a.status, "starts_on": _date(a.starts_on), "ends_on": _date(a.ends_on),
        "observed_on": _date(a.observed_on), "ambiguity": a.ambiguity,
        "source_version": pin(a.source_document)} for a in affiliations if a.source_document_id]
    mandate_rows = [{"id": m.stable_id, "person_id": m.person.stable_id, "institution": m.institution,
        "mandate_type": m.mandate_type, "territory": m.territory, "starts_on": _date(m.starts_on),
        "ends_on": _date(m.ends_on), "source_version": pin(m.source_document)} for m in mandates if m.source_document_id]
    catalogue = {"scope_scrutiny_ids": identifiers, "persons": people, "organizations": organizations,
        "affiliations": affiliate_rows, "mandates": mandate_rows, "text_versions": list(texts.values()),
        "legislative_dossiers": list(legislative.values()), "scrutinies": votes, "vote_observations": positions,
        "pagination": {"limit": limit, "offset": 0, "returned_count": len(positions),
                       "total_count_in_materialized_scope": len(positions), "complete_for_materialized_scope": True},
        "exclusions": ["Seulement les objets matérialisés du périmètre ; aucune exhaustivité institutionnelle.",
                       "Mandats et affiliations sans source exclus ; aucune appartenance historique déduite."]}
    if selection is not None:
        catalogue['selection'] = selection
        catalogue['exclusions'].append('Positions et affiliations limitées au groupe indiqué à la date du scrutin ; les totaux du scrutin restent institutionnels. Les appartenances à d’autres organisations sont hors de ce catalogue.')
    return catalogue, list(docs.values())


def build_catalogue(scrutiny_ids, *, extra_person_ids=(), extra_organization_ids=(), limit=250,
                    collective_group=None, profile='dv.catalogue/1', provenance_ruleset='dv.provenance-rules/1', binding_refs=()):
    """Legacy by default. Version 2 is an explicit documentary projection."""
    if profile not in {'dv.catalogue/1', 'dv.catalogue/2'}:
        raise ValidationError('catalogue_profile_unsupported')
    if binding_refs and (profile != 'dv.catalogue/2' or provenance_ruleset != 'dv.provenance-rules/2'):
        raise ValidationError('scrutiny_text_bindings_require_conclusion_profile')
    catalogue, refs = _build_legacy_catalogue(scrutiny_ids, extra_person_ids=extra_person_ids,
        extra_organization_ids=extra_organization_ids, limit=limit, collective_group=collective_group,
        observed_members_only=profile == 'dv.catalogue/2', binding_refs=binding_refs)
    if profile == 'dv.catalogue/2':
        from .catalogue_v2 import project_catalogue_v2
        return project_catalogue_v2(catalogue, refs, collective_group=collective_group,
                                    provenance_ruleset=provenance_ruleset, binding_refs=binding_refs)
    return catalogue, refs


SPEECH_VOTE_DERIVED_KEYS = frozenset(
    {
        "catalogue",
        "acts",
        "evidence",
        "attribution",
        "actors",
        "qualified_declaration",
        "qualification_material",
        "declaration_date_precision",
        "compared_actor_scope",
        "collective_distribution",
        "declaration_date",
        "declaration_date_state",
        "declaration_quote",
        "declaration_source_url",
        "official_text_url",
        "official_vote_url",
    }
)


def apply_speech_vote_qualifications(public: dict, comparison) -> dict:
    from .thematic_integrity import (
        collective_distribution_contract,
        qualification_material_for_comparison,
    )

    material = qualification_material_for_comparison(comparison)
    public = deepcopy(public)
    public["qualification_material"] = material
    public["qualified_declaration"] = material["qualified_declaration"]
    public["actors"] = material["actors"]
    public["compared_actor_scope"] = material["actors"]["scope"]
    qualified = material["qualified_declaration"]
    declaration = comparison.declaration
    public["declaration_date_state"] = qualified["state"]
    public["declaration_date_precision"] = qualified["precision"]
    if qualified["state"] == "known":
        public["declaration_date"] = qualified["value"]
    else:
        public["declaration_date"] = None
    collective_contract = collective_distribution_contract(material.get("collective_vote_distribution"))
    if collective_contract is not None:
        public["collective_distribution"] = collective_contract
        # Le calcul interne emploie des PK ; la projection documentaire doit
        # permettre de joindre chaque voix au même identifiant que le catalogue.
        collective = deepcopy(material["collective_vote_distribution"])
        rows = collective.get("voices", []) + collective.get("exclusions", [])
        identifiers = {row["person_id"] for row in rows if row.get("person_id") is not None}
        from atelier.models import Person
        stable = dict(Person.objects.filter(pk__in=identifiers).values_list("pk", "stable_id"))
        if identifiers - stable.keys():
            raise ValidationError("collective_person_projection_missing")
        for row in rows:
            if row.get("person_id") is not None:
                row["person_id"] = stable[row["person_id"]]
        public["qualification_material"] = {**material, "collective_vote_distribution": collective}
    public["declaration_quote"] = declaration.exact_quote
    public["declaration_source_url"] = declaration.source_passage.public_url
    act = comparison.acts.select_related("scrutiny__submitted_text").order_by("scrutiny__stable_id").first()
    if act:
        scrutiny = act.scrutiny
        public["official_text_url"] = scrutiny.submitted_text.official_url
        public["official_vote_url"] = scrutiny.official_vote_url
    return public


def frozen_comparison(comparison):
    declaration = comparison.declaration
    passage = declaration.source_passage
    evidence = {"source_version": source_ref(passage.document), "locator": passage.locator,
                "exact_quote": passage.exact_text, "transcription_status": passage.transcription_status}
    attribution = {"person_id": declaration.speaker_person.stable_id if declaration.speaker_person else None,
        "organization_id": declaration.speaker_organization.stable_id if declaration.speaker_organization else None,
        "speaker_label": declaration.speaker_label, "capacity": declaration.capacity}
    acts = []
    for act in comparison.acts.select_related("scrutiny__submitted_text", "scrutiny__source_document__source").order_by("scrutiny__stable_id"):
        s = act.scrutiny
        acts.append({"scrutiny_id": s.stable_id, "included": act.included, "reason": act.reason,
            "chronological_relation": act.chronological_relation, "occurred_on": _date(s.occurred_on),
            "official_title": s.official_title, "official_text_url": s.submitted_text.official_url,
            "official_vote_url": s.official_vote_url, "source_version": source_ref(s.source_document)})
    return {"acts": acts, "evidence": [evidence], "attribution": attribution}


def prepare_public_snapshot(kind, payload, source_snapshot, *, comparison=None, catalogue_profile=None):
    """Enrichit une nouvelle révision ; ne modifie jamais un instantané déjà approuvé."""
    public = deepcopy(payload)
    provenance_profiles = {'dv.content/0.6.0-provenance-draft.1', 'dv.content/0.7.0-conclusion-draft.1'}
    current_profile = public.get('public_contract_version')
    binding_refs = public.get('scrutiny_text_binding_refs', [])
    if binding_refs and current_profile != 'dv.content/0.7.0-conclusion-draft.1':
        raise ValidationError('scrutiny_text_bindings_require_conclusion_profile')
    if 'personal_vote_selection' in public and current_profile != 'dv.content/0.7.0-conclusion-draft.1':
        raise ValidationError('personal_vote_selection_requires_conclusion_profile')
    default_profile = 'dv.catalogue/2' if current_profile in provenance_profiles else 'dv.catalogue/1'
    profile = catalogue_profile or public.get('catalogue', {}).get('contract', default_profile)
    if profile == 'dv.catalogue/2' and current_profile not in provenance_profiles:
        raise ValidationError('catalogue_v2_requires_explicit_public_profile')
    if kind == "official_vote":
        identifiers = [public.get("scrutin_id")]
    elif kind == "vote_comparison":
        identifiers = [item.get("scrutin_id") for item in public.get("observations", [])]
    elif comparison is not None:
        frozen = frozen_comparison(comparison)
        for key, value in frozen.items():
            public.setdefault(key, value)
        identifiers = [a["scrutiny_id"] for a in frozen["acts"]]
    else:
        raise ValidationError("normalized_comparison_missing")
    extra_people, extra_orgs = [], []
    if comparison is not None:
        if comparison.declaration.speaker_person_id:
            extra_people.append(comparison.declaration.speaker_person_id)
        if comparison.declaration.speaker_organization_id:
            extra_orgs.append(comparison.declaration.speaker_organization_id)
            if comparison.declaration.position_scope == 'collective':
                # Les membres sans vote restent identifiables dans le périmètre,
                # sans création d'observation ni déduction de leur position.
                from django.db.models import Q
                for act in comparison.acts.select_related('scrutiny'):
                    date = act.scrutiny.occurred_on
                    extra_people.extend(Affiliation.objects.filter(
                        organization_id=comparison.declaration.speaker_organization_id,
                        starts_on__lte=date).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=date))
                        .values_list('person_id', flat=True))
    collective_group = None
    if comparison is not None and 'catalogue_scope' in comparison.criteria:
        if (comparison.criteria['catalogue_scope'] != {'contract': 'dv.catalogue-selection/1', 'kind': 'dated_collective_members'}
                or comparison.declaration.position_scope != 'collective'
                or not comparison.declaration.speaker_organization_id):
            raise ValidationError('collective_catalogue_invalid_scope')
        collective_group = comparison.declaration.speaker_organization
        from .diffusion import PUBLIC_CONTRACT
        if public.get('public_contract_version') not in {PUBLIC_CONTRACT, *provenance_profiles}:
            public['public_contract_version'] = 'dv.content/0.4.0-dvverite-draft.1'
    catalogue, refs = build_catalogue(identifiers, extra_person_ids=extra_people, extra_organization_ids=extra_orgs,
                                    collective_group=collective_group, profile=profile,
                                    provenance_ruleset='dv.provenance-rules/2' if current_profile == 'dv.content/0.7.0-conclusion-draft.1' else 'dv.provenance-rules/1',
                                    binding_refs=binding_refs)
    if profile == 'dv.catalogue/2':
        public['catalogue'] = catalogue
    else:
        public.setdefault("catalogue", catalogue)
    if comparison is not None and kind == "speech_vote":
        public = apply_speech_vote_qualifications(public, comparison)
    if current_profile == 'dv.content/0.7.0-conclusion-draft.1':
        from .catalogue_v2 import documentary_observations_v07
        public, documentary_refs = documentary_observations_v07(public, comparison)
        refs += documentary_refs
        if binding_refs:
            projected = {s['scrutin_id']: s for s in catalogue['scrutinies']}
            for act in public.get('acts', []):
                if act.get('scrutiny_id') in projected:
                    row = projected[act['scrutiny_id']]
                    for key in ('official_title', 'occurred_on', 'official_text_url', 'official_vote_url', 'source_version'):
                        act[key] = deepcopy(row[key])
            if len(projected) == 1:
                row = next(iter(projected.values()))
                public['official_text_url'] = row['official_text_url']
                public['official_vote_url'] = row['official_vote_url']
    # Binding checks reconstruct derived declaration/source fields first.
    from .diffusion import PUBLIC_CONTRACT, validate_diffusion_selection
    if public.get('public_contract_version') == PUBLIC_CONTRACT:
        validate_diffusion_selection(public)
    if comparison:
        refs += [source_ref(comparison.declaration.source_passage.document)]
    refs_by_version = {(r["source_id"], r["version_id"]): r for r in source_snapshot}
    for ref in refs:
        key = ref["source_id"], ref["version_id"]
        if key in refs_by_version and refs_by_version[key] != ref:
            raise ValidationError("normalized_source_conflict")
        refs_by_version[key] = ref
    return public, sorted(refs_by_version.values(), key=lambda r: (r["source_id"], r["version_id"]))


def binding_check(revision):
    if revision.dossier.data_scope == "synthetic":
        return True, "fixture_structure_only_not_documentary_verification"
    payload = revision.public_payload
    try:
        derived_keys = (
            SPEECH_VOTE_DERIVED_KEYS
            if revision.dossier.content_kind == "speech_vote"
            else {"catalogue", "acts", "evidence", "attribution"}
        )
        clean = {k: v for k, v in payload.items() if k not in derived_keys}
        derived, refs = prepare_public_snapshot(revision.dossier.content_kind, clean, revision.source_snapshot,
            comparison=revision.comparison, catalogue_profile=payload.get('catalogue', {}).get('contract', 'dv.catalogue/1'))
        if sha256_digest(derived) != sha256_digest(payload) or refs != revision.source_snapshot:
            return False, "normalized_snapshot_changed_or_unbound"
        kind = revision.dossier.content_kind
        from atelier.models import FactQualification
        scrutiny_ids=payload.get('catalogue',{}).get('scope_scrutiny_ids',[])
        for scrutiny_id in scrutiny_ids:
            qualification=FactQualification.objects.filter(scrutiny__stable_id=scrutiny_id).order_by('-created_at','-pk').first()
            if qualification and qualification.state!='qualified':
                return False,'exact_act_qualification_pending'
        if kind == "official_vote":
            scrutiny = Scrutiny.objects.get(stable_id=payload["scrutin_id"])
            fields = official_fields(scrutiny)
            for key, expected in fields.items():
                if key == "counts":
                    if any(payload[key][k].get("state") != v["state"] or payload[key][k].get("value") != v["value"] for k, v in expected.items()):
                        return False, "normalized_counts_mismatch"
                elif payload.get(key) != expected:
                    return False, "normalized_fact_mismatch:" + key
        elif kind == "vote_comparison":
            for pair in payload["observations"]:
                scrutiny = Scrutiny.objects.get(stable_id=pair["scrutin_id"])
                if pair["official_text_url"] != scrutiny.submitted_text.official_url or pair["official_vote_url"] != scrutiny.official_vote_url:
                    return False, "observation_reference_mismatch"
                for side in ("left", "right"):
                    person_id = pair.get(side + "_person_id")
                    basis = pair.get(side + "_basis")
                    if person_id:
                        values = set(VoteObservation.objects.filter(scrutiny=scrutiny, person__stable_id=person_id).values_list("normalized_category", flat=True))
                        if values != {pair[side]}:
                            return False, "observation_actor_unbound_or_conflicting"
                    elif basis != {"kind": "reference_category", "category": pair[side]}:
                        return False, "observation_basis_missing"
        elif kind == "speech_vote":
            if revision.comparison_id is None:
                return False, "comparison_missing"
            declaration = revision.comparison.declaration
            qualified = payload.get("qualified_declaration") or {}
            if payload.get("declaration_quote") != declaration.exact_quote:
                return False, "declaration_snapshot_mismatch"
            if payload.get("declaration_source_url") != declaration.source_passage.public_url:
                return False, "declaration_snapshot_mismatch"
            from .thematic_integrity import qualified_declaration_payload

            if qualified != qualified_declaration_payload(declaration):
                return False, "qualified_declaration_mismatch"
        return True, "instantané comparé aux objets normalisés et versions épinglées; pas de certification documentaire"
    except (ValidationError, Scrutiny.DoesNotExist, KeyError, TypeError, AttributeError):
        return False, "normalized_binding_missing"
