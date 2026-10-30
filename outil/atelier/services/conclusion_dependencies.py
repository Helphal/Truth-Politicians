"""Closed conclusion declarations and server-recomputed dependency rules.

The declaration names the proposition to test. It is not a proof of its
meaning. Free narrative remains B and must be checked independently against
the selected proposition, actors, objects and limits before any publication.
"""
from copy import deepcopy
from datetime import date
import re

from django.core.exceptions import ValidationError

from .canonical import sha256_digest

CONTRACT = 'dv.conclusion/1'
GRAPH_CONTRACT = 'dv.conclusion-dependencies/1'
RULESET = 'dv.dependency-rules/20261004.3'
ARCHETYPES = (
    'personal_declaration_personal_vote',
    'collective_declaration_group_distribution',
    'collective_declaration_personal_vote',
    'not_comparable_different_actor_or_capacity',
    'withdrawn_or_not_voted_object',
    'observed_vote_majority',
)
FACETS = frozenset({'archetype_claim', 'scope_limit', 'quote_context',
    'personal_relation', 'group_at_event', 'group_period', 'party_identity',
    'actor_capacity', 'object_identity', 'withdrawal', 'different_object',
    'majority_denominator'})
PREDICATES = frozenset({'speaker_identity', 'speaker_capacity',
    'target_object_identity', 'withdrawal', 'different_object',
    'group_institutional_membership', 'denominator_population'})
ACTOR_FIELDS = frozenset({'declarant_person_id', 'declarant_organization_id',
    'represented_organization_id', 'voter_person_id', 'vote_group_id'})
OBJECT_FIELDS = frozenset({'scrutiny_id', 'submitted_text_id',
    'target_object_id', 'target_object_kind'})
DECLARATION_FIELDS = frozenset({'contract', 'archetype', 'actors', 'objects',
    'temporal_scope', 'denominator', 'narrative_bindings', 'evidence_bindings'})


def _resolve(snapshot, location):
    if not isinstance(location, str) or not location.startswith('/'):
        raise ValidationError('conclusion_location_required')
    value = snapshot.get('content', {})
    try:
        for raw in location[1:].split('/'):
            part = raw.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValidationError('conclusion_location_missing') from None
    return value


def _payload(snapshot):
    value = snapshot.get('content', {}).get('public_payload', {})
    if not isinstance(value, dict):
        raise ValidationError('conclusion_public_payload_required')
    return value


def _identifier(value):
    return value is None or (type(value) is str and bool(value.strip()))


def _actors(snapshot):
    actors = _payload(snapshot).get('actors', {})
    speaker = actors.get('speaker', {})
    represented = actors.get('represented_entity', {})
    compared = actors.get('compared_actor', {})
    if not all(isinstance(x, dict) for x in (actors, speaker, represented, compared)):
        raise ValidationError('conclusion_structured_actors_required')
    return {
        'declarant_person_id': speaker.get('person_id'),
        'declarant_organization_id': speaker.get('organization_id'),
        'represented_organization_id': represented.get('organization_id'),
        'voter_person_id': compared.get('person_id'),
        'vote_group_id': compared.get('organization_id') or represented.get('organization_id'),
    }


def _scrutiny(snapshot):
    payload = _payload(snapshot)
    catalogue = payload.get('catalogue', {})
    rows = catalogue.get('scrutinies', [])
    acts = payload.get('acts', [])
    selected = {a.get('scrutiny_id') for a in acts if isinstance(a, dict) and a.get('included') is True}
    if not selected:
        explicit = payload.get('qualification_material', {}).get('scrutiny_id')
        if explicit:
            selected = {explicit}
        else:
            selected = {s.get('scrutin_id', s.get('scrutiny_id')) for s in rows if isinstance(s, dict)}
    selected.discard(None)
    if len(selected) != 1:
        raise ValidationError('conclusion_exact_single_scrutiny_required')
    identifier = next(iter(selected))
    matches = [(i, s) for i, s in enumerate(rows) if isinstance(s, dict)
               and s.get('scrutin_id', s.get('scrutiny_id')) == identifier]
    if len(matches) != 1:
        raise ValidationError('conclusion_scrutiny_not_exact_catalogue_object')
    return matches[0]


def _narratives(snapshot):
    # Bind every free narrative scalar outside the catalogue. Catalogue scalars
    # are routed separately; adding/changing a reserve changes this commitment.
    result = {}
    from . import verification
    for claim in verification._scalars(snapshot.get('content', {})):
        path, value = claim['location'], claim['value']
        if type(value) is not str or not value.strip():
            continue
        if re.match(r'^/(?:title|short_answer|main_caveat|reasoning|result)(?:/|$)', path) or re.match(
            r'^/public_payload/(?:title|short_answer|main_caveat|method_explanation|reasoning|scope|result|counterevidence|declaration_quote)(?:/|$)', path):
            result[path] = value
        elif re.fullmatch(r'/public_payload/themes/\d+/(?:label|selection_reason)', path):
            result[path] = value
        elif re.fullmatch(r'/public_payload/(?:actors/speaker|attribution)/capacity', path):
            result[path] = value
    return result


def _base_facets(location):
    if location.endswith('/declaration_quote'):
        return {'quote_context'}
    if location.endswith('/capacity'):
        return {'actor_capacity'}
    if '/main_caveat' in location or '/method_explanation' in location or '/scope' in location:
        return {'scope_limit'}
    return {'archetype_claim'}


def _population(snapshot, actors, scrutiny_id):
    rows = _payload(snapshot).get('catalogue', {}).get('vote_observations', [])
    return sorted({r.get('person_id') for r in rows if isinstance(r, dict)
        and r.get('scrutiny_id') == scrutiny_id and r.get('person_id')
        and r.get('affiliation_at_event_id', r.get('organization_id')) == actors['vote_group_id']})


def build_conclusion(snapshot, *, archetype, temporal_scope=None, denominator=None,
                     target_object_id=None, target_object_kind=None,
                     narrative_facets=None, evidence_bindings=()):
    if archetype not in ARCHETYPES:
        raise ValidationError('conclusion_archetype_required')
    _, scrutiny = _scrutiny(snapshot)
    scrutiny_id = scrutiny.get('scrutin_id', scrutiny.get('scrutiny_id'))
    actors = _actors(snapshot)
    facets = narrative_facets or {}
    narratives = _narratives(snapshot)
    if not isinstance(facets, dict) or not set(facets) <= set(narratives):
        raise ValidationError('conclusion_narrative_facet_location_invalid')
    bindings = []
    for path, value in narratives.items():
        chosen = facets.get(path, [])
        if not isinstance(chosen, (list, tuple)) or any(type(x) is not str or x not in FACETS for x in chosen):
            raise ValidationError('conclusion_narrative_facet_invalid')
        bindings.append({'location': path, 'value_sha256': sha256_digest(value),
                         'facets': sorted(_base_facets(path) | set(chosen))})
    if archetype == 'observed_vote_majority' and denominator is None:
        denominator = {'kind': 'listed_vote_positions', 'group_id': actors['vote_group_id'],
            'scrutiny_id': scrutiny_id, 'numerator_category': 'for',
            'member_person_ids': _population(snapshot, actors, scrutiny_id)}
    declaration = {'contract': CONTRACT, 'archetype': archetype, 'actors': actors,
        'objects': {'scrutiny_id': scrutiny_id,
                    'submitted_text_id': scrutiny.get('submitted_text_id', scrutiny.get('text_version_id')),
                    'target_object_id': target_object_id, 'target_object_kind': target_object_kind},
        'temporal_scope': deepcopy(temporal_scope) if temporal_scope is not None else {
            'kind': 'at_scrutiny', 'scrutiny_id': scrutiny_id, 'starts_on': None, 'ends_on': None},
        'denominator': deepcopy(denominator), 'narrative_bindings': bindings,
        'evidence_bindings': deepcopy(list(evidence_bindings))}
    return validate_conclusion(snapshot, declaration)


def validate_conclusion(snapshot, declaration):
    if not isinstance(declaration, dict) or set(declaration) != DECLARATION_FIELDS or declaration.get('contract') != CONTRACT:
        raise ValidationError('conclusion_closed_contract_required')
    if declaration['archetype'] not in ARCHETYPES:
        raise ValidationError('conclusion_archetype_required')
    actors = declaration['actors']
    if (not isinstance(actors, dict) or set(actors) != ACTOR_FIELDS
            or any(not _identifier(v) for v in actors.values()) or actors != _actors(snapshot)):
        raise ValidationError('conclusion_actor_binding_changed')
    objects = declaration['objects']
    _, scrutiny = _scrutiny(snapshot)
    scrutiny_id = scrutiny.get('scrutin_id', scrutiny.get('scrutiny_id'))
    if (not isinstance(objects, dict) or set(objects) != OBJECT_FIELDS
            or objects['scrutiny_id'] != scrutiny_id
            or objects['submitted_text_id'] != scrutiny.get('submitted_text_id', scrutiny.get('text_version_id'))
            or not _identifier(objects['target_object_id'])
            or objects['target_object_kind'] not in (None, 'amendment', 'motion', 'whole_text')):
        raise ValidationError('conclusion_object_binding_changed')
    if declaration['archetype'] == 'withdrawn_or_not_voted_object' and not objects['target_object_id']:
        raise ValidationError('conclusion_exact_withdrawn_target_required')
    temporal = declaration['temporal_scope']
    if (not isinstance(temporal, dict) or set(temporal) != {'kind', 'scrutiny_id', 'starts_on', 'ends_on'}
            or temporal['kind'] not in ('at_scrutiny', 'period') or temporal['scrutiny_id'] != scrutiny_id):
        raise ValidationError('conclusion_temporal_scope_invalid')
    for key in ('starts_on', 'ends_on'):
        if temporal[key] is not None:
            try:
                date.fromisoformat(temporal[key])
            except (TypeError, ValueError):
                raise ValidationError('conclusion_temporal_date_invalid') from None
    if temporal['kind'] == 'at_scrutiny' and (temporal['starts_on'] is not None or temporal['ends_on'] is not None):
        raise ValidationError('observation_cannot_declare_membership_period')
    if temporal['starts_on'] and temporal['ends_on'] and temporal['starts_on'] > temporal['ends_on']:
        raise ValidationError('conclusion_temporal_interval_reversed')
    narratives = _narratives(snapshot)
    entries = declaration['narrative_bindings']
    if not isinstance(entries, list) or len(entries) != len(narratives):
        raise ValidationError('conclusion_complete_narrative_binding_required')
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {'location', 'value_sha256', 'facets'}:
            raise ValidationError('conclusion_narrative_binding_invalid')
        path = item['location']
        facets = item['facets']
        if (path in seen or path not in narratives or item['value_sha256'] != sha256_digest(narratives[path])
                or not isinstance(facets, list) or any(type(f) is not str for f in facets) or facets != sorted(set(facets))
                or not set(facets) <= FACETS or not _base_facets(path) <= set(facets)):
            raise ValidationError('conclusion_narrative_binding_changed')
        seen.add(path)
    denominator = declaration['denominator']
    if declaration['archetype'] == 'observed_vote_majority' and denominator is None:
        raise ValidationError('conclusion_majority_denominator_required')
    if denominator is not None:
        if (not isinstance(denominator, dict) or set(denominator) != {'kind', 'group_id', 'scrutiny_id', 'numerator_category', 'member_person_ids'}
                or denominator['kind'] not in ('listed_vote_positions', 'observed_group_members')
                or denominator['group_id'] != actors['vote_group_id'] or denominator['scrutiny_id'] != scrutiny_id
                or not denominator['group_id'] or denominator['numerator_category'] not in ('for', 'against', 'abstention')
                or denominator['member_person_ids'] != _population(snapshot, actors, scrutiny_id)):
            raise ValidationError('conclusion_denominator_not_exact_server_population')
    bindings = declaration['evidence_bindings']
    if not isinstance(bindings, list):
        raise ValidationError('conclusion_evidence_bindings_required')
    seen = set()
    for item in bindings:
        if (not isinstance(item, dict) or set(item) != {'predicate', 'location', 'value_sha256'}
                or type(item['predicate']) is not str or item['predicate'] not in PREDICATES
                or type(item['location']) is not str or (item['predicate'], item['location']) in seen
                or item['value_sha256'] != sha256_digest(_resolve(snapshot, item['location']))):
            raise ValidationError('conclusion_evidence_binding_changed')
        if item['predicate'] == 'speaker_capacity' and item['location'] not in {
                '/public_payload/actors/speaker/capacity', '/public_payload/attribution/capacity'}:
            raise ValidationError('conclusion_capacity_requires_exact_actor_field')
        if item['predicate'] in {'target_object_identity', 'withdrawal'}:
            field = 'object_id' if item['predicate'] == 'target_object_identity' else 'status'
            if not re.fullmatch(r'/public_payload/object_observations/\d+/' + field, item['location']):
                raise ValidationError('conclusion_object_predicate_requires_closed_observation')
            observation = _resolve(snapshot, item['location'].rsplit('/', 1)[0])
            if (not isinstance(observation, dict) or observation.get('object_id') != objects['target_object_id']
                    or observation.get('object_kind') != objects['target_object_kind']):
                raise ValidationError('conclusion_object_observation_is_not_selected_target')
        seen.add((item['predicate'], item['location']))
    return deepcopy(declaration)


make_conclusion = build_conclusion


def _rule(identifier, required, why, evidence, *, model=False, new_source=False):
    return {'required': bool(required), 'dependency_rule_id': RULESET + ':' + identifier,
        'why_required_for_this_conclusion': why,
        'what_evidence_would_resolve_it': evidence,
        'can_resolve_without_model': not model,
        'can_resolve_without_new_source': not new_source}


def _source_key(reference):
    if not isinstance(reference, dict):
        return None
    return (reference.get('source_id'), reference.get('version_id'), reference.get('sha256'))


def _sources(snapshot):
    integrities = {'original_and_passages_checked'}
    if snapshot.get('scope') == 'synthetic':
        integrities.add('synthetic_no_original')
    return {_source_key(s.get('reference')): s for s in snapshot.get('sources', [])
        if s.get('state') == 'available' and s.get('integrity') in integrities}


def _proven_at(classification, location, fact_types=None):
    matches = [c for c in classification['claims'] if c['location'] == location
        and c['layer'] == 'deterministic' and c.get('provenance_status') == 'proven'
        and c.get('fact_type') != 'projection_binding'
        and (fact_types is None or c.get('fact_type') in fact_types)]
    return len(matches) == 1


def _bound_proof(classification, declaration, predicate, types, *, snapshot=None, expected_value=None):
    bindings = [b for b in declaration['evidence_bindings'] if b['predicate'] == predicate]
    return bool(bindings) and all(_proven_at(classification, b['location'], types)
        and (snapshot is None or expected_value is None or _resolve(snapshot, b['location']) == expected_value)
        for b in bindings)


def _same_original_record(classification, declaration, left, right):
    certificates = {cert['location']: cert for cert in (classification.get('provenance_report') or {}).get('certificates', [])}
    left_bindings = [b for b in declaration['evidence_bindings'] if b['predicate'] == left]
    right_bindings = [b for b in declaration['evidence_bindings'] if b['predicate'] == right]
    if len(left_bindings) != 1 or len(right_bindings) != 1:
        return False
    a, b = left_bindings[0], right_bindings[0]
    if a['location'].rsplit('/', 1)[0] != b['location'].rsplit('/', 1)[0]:
        return False
    a, b = certificates.get(a['location']), certificates.get(b['location'])
    if not a or not b:
        return False
    return bool(a.get('record_id') and all(a.get(key) == b.get(key)
        for key in ('source_id', 'version_id', 'source_sha256', 'record_id')))


def _capacity_actor_bound(snapshot, classification, declaration):
    certificates = {cert['location']: cert for cert in (classification.get('provenance_report') or {}).get('certificates', [])}
    payload = _payload(snapshot)
    expected = payload.get('actors', {}).get('speaker', {}).get('label')
    attribution_label = payload.get('attribution', {}).get('speaker_label')
    if not expected or (attribution_label and attribution_label != expected):
        return False
    bindings = [b for b in declaration['evidence_bindings'] if b['predicate'] == 'speaker_capacity']
    return bool(bindings) and all(certificates.get(b['location'], {}).get('record_locator', {}).get('speaker_label') == expected
                                 for b in bindings)


def _collective_machine_projection(snapshot, classification, scrutiny, group_id):
    """Recognize a closed, recomputable contextual table, never its prose.

    qualification_material is an open historical surface. A prefix therefore
    cannot exempt its contents. Only this producer's exact shape, original-
    certified event positions and matching counts/source tuples are accepted.
    Partial distributions, conflicts, exclusions or extensions stay required.
    Recognition is a projection check, not institutional exhaustiveness or a
    semantic PASS; the reserve always remains independently judged B.
    """
    payload = _payload(snapshot)
    catalogue = payload.get('catalogue', {})
    material = payload.get('qualification_material', {}).get('collective_vote_distribution')
    contract = payload.get('collective_distribution')
    result = {'status': 'not_reconstructed', 'field_locations': [],
              'reason': 'Projection collective non reconnue exactement ; aucun champ exempté.'}
    if material is None and contract is None:
        return {**result, 'status': 'not_present'}
    fields = {'state', 'rule_id', 'rule_version', 'as_of', 'affiliations',
              'usable_observations', 'usable_voices', 'distribution', 'voices', 'exclusions', 'reserve'}
    contract_fields = {'state', 'rule_id', 'rule_version', 'as_of',
                       'included_member_ids', 'excluded_member_ids', 'unknown_member_count'}
    if (payload.get('public_contract_version') != 'dv.content/0.7.0-conclusion-draft.1'
            or catalogue.get('contract') != 'dv.catalogue/2'
            or catalogue.get('projection_ruleset') != 'dv.provenance-rules/2'
            or not isinstance(material, dict) or set(material) != fields
            or not isinstance(contract, dict) or set(contract) != contract_fields
            or not group_id or material.get('state') != 'group_distribution'
            or material.get('rule_id') != 'COLLECTIVE_AFFILIATION_DATED_V1'
            or material.get('rule_version') != 'racc001-1'
            or material.get('as_of') != scrutiny.get('occurred_on')
            or not isinstance(material.get('reserve'), str)
            or material.get('exclusions') != []
            or not isinstance(material.get('voices'), list)
            or not isinstance(material.get('distribution'), dict)
            or any(type(material.get(k)) is not int for k in
                   ('affiliations', 'usable_observations', 'usable_voices'))):
        return result
    proven = {}
    for claim in classification['claims']:
        if (claim['layer'] == 'deterministic' and claim.get('provenance_status') == 'proven'
                and claim.get('fact_type') == 'vote_position'):
            proven.setdefault(claim['location'], []).append(claim)
    sources = _sources(snapshot)
    sid = scrutiny.get('scrutin_id', scrutiny.get('scrutiny_id'))
    rows = [(i, r) for i, r in enumerate(catalogue.get('vote_observations', []))
            if isinstance(r, dict) and r.get('scrutiny_id') == sid
            and r.get('affiliation_at_event_id') == group_id]
    if not rows:
        return result
    by_person = {}
    for index, row in rows:
        ref, category, person = row.get('source_version'), row.get('normalized_category'), row.get('person_id')
        prefix = f'/public_payload/catalogue/vote_observations/{index}/'
        if (not isinstance(person, str) or not person
                or category not in {'for', 'against', 'abstention', 'non_voting'}
                or _source_key(ref) not in sources or sources[_source_key(ref)]['reference'] != ref
                or ref not in snapshot.get('content', {}).get('source_snapshot', [])
                or any(len(proven.get(prefix + field, [])) != 1 for field in
                       ('person_id', 'scrutiny_id', 'affiliation_at_event_id', 'normalized_category'))):
            return result
        by_person.setdefault(person, []).append({
            'source_id': ref['source_id'], 'version_id': ref['version_id'],
            'sha256': ref['sha256'], 'category': category})
    seen, distribution = [], {}
    source_fields = {'source_id', 'version_id', 'sha256', 'category'}
    for voice in material['voices']:
        if (not isinstance(voice, dict) or set(voice) != {'person_id', 'category', 'sources', 'observation_count'}
                or not isinstance(voice.get('person_id'), str) or voice['person_id'] in seen
                or voice['person_id'] not in by_person or type(voice['observation_count']) is not int
                or not isinstance(voice.get('category'), str)
                or not isinstance(voice['sources'], list)
                or any(not isinstance(s, dict) or set(s) != source_fields for s in voice['sources'])):
            return result
        expected = by_person[voice['person_id']]
        categories = {s['category'] for s in expected}
        if (categories != {voice['category']} or voice['observation_count'] != len(expected)
                or sorted(sha256_digest(s) for s in voice['sources']) != sorted(sha256_digest(s) for s in expected)):
            return result
        seen.append(voice['person_id'])
        distribution[voice['category']] = distribution.get(voice['category'], 0) + 1
    if (set(seen) != set(by_person) or material['affiliations'] != len(seen)
            or material['usable_voices'] != len(seen) or material['usable_observations'] != len(rows)
            or any(type(n) is not int for n in material['distribution'].values())
            or material['distribution'] != distribution
            or contract != {'state': 'available', 'rule_id': material['rule_id'],
                'rule_version': material['rule_version'], 'as_of': material['as_of'],
                'included_member_ids': seen, 'excluded_member_ids': [], 'unknown_member_count': None}):
        return result
    from .verification import _scalars
    paths = [c['location'] for c in _scalars({k: v for k, v in material.items() if k != 'reserve'},
        '/public_payload/qualification_material/collective_vote_distribution')]
    paths += [f'/public_payload/collective_distribution/included_member_ids/{i}' for i in range(len(seen))]
    return {'status': 'reconstructed', 'field_locations': sorted(paths),
            'person_count': len(seen), 'observation_count': len(rows),
            'reason': 'Champs machine reconstruits depuis les positions originales reçues ; réserve sémantique conservée.'}


def _submitted_text_binding(snapshot, scrutiny, text_indexes):
    """Read an explicit append-only selection; a procedural record is not text.

    Unselected historical FK projections keep their former route. An explicit
    new selector cannot self-declare literal reception or alias its objects.
    """
    payload = _payload(snapshot)
    refs = payload.get('scrutiny_text_binding_refs', [])
    result = {'selected': False, 'status': 'not_selected',
              'literal_text_status': None, 'literal_document_established': None,
              'reason': 'Aucune liaison append-only sélectionnée ; route documentaire du catalogue conservée.'}
    if not refs:
        return result
    catalogue = payload.get('catalogue', {})
    sid = scrutiny.get('scrutin_id', scrutiny.get('scrutiny_id'))
    try:
        from .catalogue_v2 import select_scrutiny_text_bindings
        ids = [row.get('scrutin_id', row.get('scrutiny_id')) for row in catalogue.get('scrutinies', [])]
        selected = select_scrutiny_text_bindings(ids, refs)
        binding = selected.get(sid)
        if binding is None:
            raise ValidationError('selected_conclusion_scrutiny_text_binding_missing')
        received = binding.payload
        sources = _sources(snapshot)
        if (scrutiny.get('submitted_text_id') != received['submitted_text_id']
                or scrutiny.get('source_version') != received['source_version']
                or scrutiny.get('procedure') != received['procedure']
                or not text_indexes
                or any(catalogue['text_versions'][i].get('source_version') != received['text_source_version']
                       or catalogue['text_versions'][i].get('dossier_id') != received['legislative_dossier_id']
                       for i in text_indexes)
                or any(_source_key(received[key]) not in sources
                       or sources[_source_key(received[key])].get('reference') != received[key]
                       or received[key] not in snapshot.get('content', {}).get('source_snapshot', []) for key in
                       ('source_version', 'text_source_version', 'dossier_source_version'))):
            raise ValidationError('selected_scrutiny_text_binding_snapshot_changed')
        literal = received['literal_text_status'] == 'received_literal_document'
        return {'selected': True, 'status': 'validated',
            'selection': {'scrutiny_id': sid, 'version': binding.version, 'payload_digest': binding.payload_digest},
            'submitted_text_id': received['submitted_text_id'],
            'literal_text_status': received['literal_text_status'],
            'literal_document_established': literal,
            'reason': 'Version littérale autonome reçue et liaison revalidée.' if literal
                else 'Liaison procédurale reçue ; le document littéral soumis reste non reçu ou inconnu.'}
    except ValidationError as exc:
        return {'selected': True, 'status': 'invalid', 'literal_text_status': None,
                'literal_document_established': False, 'reason': '; '.join(exc.messages)}


def build_dependency_graph(snapshot, classification, conclusion=None):
    """Recompute necessity and documentary dependencies from a closed declaration.

    The caller must validate classification/provenance before relying on the
    returned graph. No necessary flag from that report or a client is read.
    """
    declaration = validate_conclusion(snapshot, conclusion if conclusion is not None else snapshot.get('conclusion'))
    if classification.get('fingerprint') != sha256_digest(snapshot):
        raise ValidationError('dependency_classification_fingerprint_changed')
    payload = _payload(snapshot)
    catalogue = payload.get('catalogue', {})
    actors, objects = declaration['actors'], declaration['objects']
    archetype = declaration['archetype']
    is_noncomparable = archetype == 'not_comparable_different_actor_or_capacity'
    is_withdrawn = archetype == 'withdrawn_or_not_voted_object'
    group_archetype = archetype in {'collective_declaration_group_distribution',
                                   'collective_declaration_personal_vote', 'observed_vote_majority'}
    facets = {f for b in declaration['narrative_bindings'] for f in b['facets']}
    group_required = group_archetype or bool(facets & {'group_at_event', 'group_period', 'party_identity'})
    group_positions_required = archetype in {'collective_declaration_group_distribution', 'observed_vote_majority'}
    period_required = declaration['temporal_scope']['kind'] == 'period' or 'group_period' in facets
    personal_vote_required = archetype in {'personal_declaration_personal_vote', 'collective_declaration_personal_vote'}
    # A whole-vote reference establishes its own object. It does not establish
    # a submitted version for an amendment that was withdrawn without a vote.
    submitted_required = not (is_noncomparable or is_withdrawn)
    population_required = bool(declaration['denominator'] and declaration['denominator']['kind'] == 'observed_group_members')
    institutional_exhaustiveness = period_required or population_required
    sources = _sources(snapshot)
    person_ids = {x for x in (actors['declarant_person_id'], actors['voter_person_id']) if x}
    if is_noncomparable or is_withdrawn:
        person_ids = set()
    group_id = actors['vote_group_id']
    scrutiny_index, scrutiny = _scrutiny(snapshot)
    sid = objects['scrutiny_id']
    position_indexes = [i for i, row in enumerate(catalogue.get('vote_observations', []))
        if isinstance(row, dict) and row.get('scrutiny_id') == sid and (
            (group_positions_required and row.get('affiliation_at_event_id', row.get('organization_id')) == group_id)
            or (personal_vote_required and row.get('person_id') == actors['voter_person_id']))]
    if group_required:
        person_ids |= {catalogue['vote_observations'][i].get('person_id') for i in position_indexes}
        person_ids.discard(None)
    person_indexes = {i for i, row in enumerate(catalogue.get('persons', [])) if row.get('id') in person_ids}
    organization_ids = {group_id} if group_required and group_id else set()
    if 'party_identity' in facets:
        organization_ids |= {x for x in (actors['declarant_organization_id'], actors['represented_organization_id']) if x}
    organization_indexes = {i for i, row in enumerate(catalogue.get('organizations', [])) if row.get('id') in organization_ids}
    affiliation_indexes = {i for i, row in enumerate(catalogue.get('affiliations', []))
        if group_required and row.get('person_id') in person_ids and row.get('organization_id') == group_id
        and (period_required or (row.get('temporal_kind') == 'observed_at_scrutiny'
             and row.get('observed_on') == scrutiny.get('occurred_on')))}
    text_indexes = {i for i, row in enumerate(catalogue.get('text_versions', []))
                    if submitted_required and row.get('id') == objects['submitted_text_id']}
    binding_locations = {b['location'] for b in declaration['evidence_bindings']}
    collective_projection = _collective_machine_projection(snapshot, classification, scrutiny, group_id)
    projection_required = group_positions_required or population_required or 'majority_denominator' in facets
    collective_projection['required_for_conclusion'] = projection_required
    contextual_fields = set(collective_projection['field_locations']) if not projection_required else set()

    def required_location(path, layer):
        if path in binding_locations:
            return True, 'explicit_evidence_predicate'
        if path in contextual_fields:
            return False, 'reconstructed_group_projection_outside_individual_conclusion'
        match = re.match(r'^/public_payload/catalogue/(persons|organizations|vote_observations|affiliations|scrutinies|text_versions)/(\d+)/(.*)$', path)
        if match:
            family, raw_index, field = match.groups()
            index = int(raw_index)
            if family == 'persons':
                return index in person_indexes and (field in {'id', 'display_name'} or field.startswith('institutional_ids/')), 'selected_person_identity'
            if family == 'organizations':
                return index in organization_indexes and field in {'id', 'name', 'kind'}, 'selected_organization_identity'
            if family == 'vote_observations':
                needed_fields = {'person_id', 'scrutiny_id', 'normalized_category', 'source_category'}
                if group_required:
                    needed_fields |= {'affiliation_at_event_id', 'organization_id'}
                return index in position_indexes and field in needed_fields, 'exact_vote_position_at_event'
            if family == 'affiliations':
                fields = {'person_id', 'organization_id', 'observed_on'} if not period_required else {'person_id', 'organization_id', 'starts_on', 'ends_on'}
                # Historical received bounds never establish actual membership.
                if field.startswith('legacy_bounds/'):
                    return False, 'historical_received_bound_not_membership_fact'
                return index in affiliation_indexes and field in fields, 'institutional_membership_period' if period_required else 'group_observation_at_exact_event'
            if family == 'scrutinies':
                count = field.startswith('counts/')
                return index == scrutiny_index and (not count or population_required), 'exact_vote_object_and_date'
            if family == 'text_versions':
                return index in text_indexes, 'exact_submitted_text'
        if path.startswith('/public_payload/catalogue/'):
            return False, 'catalogue_context_outside_selected_conclusion'
        if layer == 'semantic':
            return True, 'independent_meaning_and_conclusion_consistency'
        if layer == 'editorial':
            return False, 'editorial_selection_not_external_proof'
        if layer == 'unknown':
            return True, 'explicit_unknown_outside_catalogue_required_by_default'
        # Noncatalogue certified facts are retained when they are documentary
        # actors/objects/evidence. Technical projection metadata is never truth.
        return bool(re.match(r'^/public_payload/(?:actors|attribution|evidence|qualified_declaration|qualification_material)(?:/|$)', path)), 'documentary_binding_for_selected_conclusion'

    necessities = []
    for record in [*classification['claims'], *classification.get('unknowns', [])]:
        needed, rule = required_location(record['location'], record.get('layer', 'unknown'))
        if record.get('fact_type') == 'projection_binding':
            needed, rule = False, 'projection_metadata_not_external_fact'
        necessities.append({'claim_id': record['claim_id'], 'location': record['location'],
            'necessary_for_conclusion': needed, 'dependency_rule_id': RULESET + ':' + rule,
            'applicability': 'required' if needed else 'not_applicable'})
    necessities = list({(n['claim_id'], n['location']): n for n in necessities}.values())
    required_ids = {n['claim_id'] for n in necessities if n['necessary_for_conclusion']}
    unresolved = [c for c in classification['claims'] if c['claim_id'] in required_ids
        and (c['layer'] == 'unknown' or (c['layer'] == 'deterministic' and c.get('provenance_status') != 'proven'))]
    routed_ids = {c['claim_id'] for c in classification['claims']}
    unresolved += [u for u in classification.get('unknowns', []) if u['claim_id'] in required_ids and u['claim_id'] not in routed_ids]
    evidence = payload.get('evidence', [])
    evidence = evidence if isinstance(evidence, list) else [evidence]
    received = [e for e in evidence if isinstance(e, dict) and _source_key(e.get('source_version')) in sources]
    source_ok = bool(received)
    quote_ok = source_ok and all(isinstance(e.get('exact_quote'), str) and e['exact_quote'].strip()
        and isinstance(e.get('locator'), str) and e['locator'].strip()
        and any(e['exact_quote'] in p['exact_text'] and e['locator'] == p['locator']
                for p in sources[_source_key(e['source_version'])].get('passages', [])) for e in received)
    vote_prefix = f'/public_payload/catalogue/scrutinies/{scrutiny_index}/'
    vote_ok = _proven_at(classification, vote_prefix + 'scrutin_id', {'scrutiny_identity', 'scrutiny_id'})
    vote_source_ok = _source_key(scrutiny.get('source_version')) in sources
    object_ok = vote_ok and vote_source_ok and bool(scrutiny.get('official_title')) and bool(scrutiny.get('procedure')) and scrutiny.get('object_state', 'known') == 'known'
    version_ok = (bool(text_indexes) and scrutiny.get('text_reference_role') == 'submitted'
        and all(_source_key(catalogue['text_versions'][i].get('source_version')) in sources for i in text_indexes))
    # Readback uses all bound text objects even for a documentary archetype
    # where submitted text is not required; missing literal text then stays NA.
    binding_text_indexes = {i for i, row in enumerate(catalogue.get('text_versions', []))
                            if row.get('id') == objects['submitted_text_id']}
    submitted_binding = _submitted_text_binding(snapshot, scrutiny, binding_text_indexes)
    if submitted_binding['selected']:
        # An explicit invalid object selection is never an exemption merely
        # because this conclusion does not require the literal submitted text.
        object_ok = object_ok and submitted_binding['status'] == 'validated'
        version_ok = version_ok and submitted_binding['literal_document_established'] is True
    actor_ok = True
    if personal_vote_required:
        wanted = actors['voter_person_id']
        matching = [i for i in position_indexes if catalogue['vote_observations'][i].get('person_id') == wanted]
        actor_ok = bool(wanted and len(matching) == 1)
        for i in matching:
            prefix = f'/public_payload/catalogue/vote_observations/{i}/'
            actor_ok = actor_ok and all(_proven_at(classification, prefix + field) for field in ('person_id', 'scrutiny_id', 'normalized_category'))
            actor_ok = actor_ok and _source_key(catalogue['vote_observations'][i].get('source_version')) in sources
    if group_required:
        actor_ok = actor_ok and bool(group_id and position_indexes)
        for i in position_indexes:
            prefix = f'/public_payload/catalogue/vote_observations/{i}/'
            row = catalogue['vote_observations'][i]
            field = 'affiliation_at_event_id' if 'affiliation_at_event_id' in row else 'organization_id'
            actor_ok = actor_ok and all(_proven_at(classification, prefix + f) for f in ('person_id', 'scrutiny_id', 'normalized_category', field))
        # Collective speech to personal vote requires a received institutional
        # relation for this person at this exact vote; no group-name heuristic.
        if archetype == 'collective_declaration_personal_vote':
            actor_ok = actor_ok and any(catalogue['vote_observations'][i].get('person_id') == actors['voter_person_id']
                and catalogue['vote_observations'][i].get('affiliation_at_event_id', catalogue['vote_observations'][i].get('organization_id')) == group_id for i in position_indexes)
    if 'party_identity' in facets:
        actor_ok = actor_ok and any(catalogue['organizations'][i].get('kind') == 'party'
            and all(_proven_at(classification, f'/public_payload/catalogue/organizations/{i}/' + field)
                    for field in ('id', 'name', 'kind')) for i in organization_indexes)
    if period_required:
        relevant = [i for i in affiliation_indexes if catalogue['affiliations'][i].get('person_id') in {actors['declarant_person_id'], actors['voter_person_id']}]
        scope = declaration['temporal_scope']
        period_ok = bool(relevant and scope['starts_on'] and scope['ends_on'])
        for i in relevant:
            row = catalogue['affiliations'][i]
            prefix = f'/public_payload/catalogue/affiliations/{i}/'
            period_ok = period_ok and row.get('temporal_kind') != 'observed_at_scrutiny'
            period_ok = period_ok and _proven_at(classification, prefix + 'starts_on', {'affiliation_start', 'membership_start_date'})
            period_ok = period_ok and _proven_at(classification, prefix + 'ends_on', {'affiliation_end', 'membership_end_date'})
            period_ok = period_ok and bool(row.get('starts_on') and row.get('ends_on') and row['starts_on'] <= scope['starts_on'] and row['ends_on'] >= scope['ends_on'])
        actor_ok = actor_ok and period_ok
    personal_same = (actors['declarant_person_id'] is not None
        and actors['declarant_person_id'] == actors['voter_person_id']
        and payload.get('actors', {}).get('scope') not in {'government', 'party'})
    author_ok = personal_same if archetype == 'personal_declaration_personal_vote' else True
    capacity_ok = True
    if is_noncomparable:
        capacity_ok = (_bound_proof(classification, declaration, 'speaker_capacity', {'declared_actor_capacity'})
                       and _capacity_actor_bound(snapshot, classification, declaration))
        # The identity may remain unresolved. A source-attributed capacity and
        # the chamber/object separation is a different documentary proposition.
        distinct_ids = bool(actors['declarant_person_id'] and actors['voter_person_id']
                            and actors['declarant_person_id'] != actors['voter_person_id'])
        capacities = [_resolve(snapshot, b['location']) for b in declaration['evidence_bindings'] if b['predicate'] == 'speaker_capacity']
        # This branch uses the source-derived closed capacity certificate. A
        # free 'government' scope or an unresolved UID supplies no such fact.
        distinct_capacity = (capacity_ok and sid.startswith(('AN:', 'SENAT:'))
            and capacities and all(isinstance(value, str) and re.fullmatch(r'ministre(?:[^.]{0,120})?', value)
                                  for value in capacities))
        actor_ok = capacity_ok and vote_ok and vote_source_ok and (distinct_ids or distinct_capacity)
    withdrawal_ok = True
    if is_withdrawn:
        identity_ok = _bound_proof(classification, declaration, 'target_object_identity', {'parliamentary_object_identity', 'text_identity', 'amendment_identity'}, snapshot=snapshot, expected_value=objects['target_object_id'])
        withdrawal_bindings = [b for b in declaration['evidence_bindings'] if b['predicate'] == 'withdrawal']
        withdrawal_status = _resolve(snapshot, withdrawal_bindings[0]['location']) if len(withdrawal_bindings) == 1 else None
        withdrawn_proof = (withdrawal_status in {'withdrawn', 'not_submitted_to_vote'}
            and _bound_proof(classification, declaration, 'withdrawal', {'object_withdrawal', 'amendment_withdrawal', 'object_not_submitted_to_vote'}, snapshot=snapshot, expected_value=withdrawal_status))
        same_object_record = _same_original_record(classification, declaration, 'target_object_identity', 'withdrawal')
        observation_ok = False
        if len(withdrawal_bindings) == 1:
            observation = _resolve(snapshot, withdrawal_bindings[0]['location'].rsplit('/', 1)[0])
            source = sources.get(_source_key(observation.get('source_version')))
            observation_ok = bool(source and observation.get('exact_quote') and observation.get('locator')
                and any(observation['exact_quote'] in p['exact_text'] and observation['locator'] == p['locator']
                        for p in source.get('passages', [])))
        distinct = objects['target_object_id'] != objects['submitted_text_id'] and objects['target_object_id'] != sid
        withdrawal_ok = identity_ok and withdrawn_proof and distinct and same_object_record and observation_ok
    if population_required:
        actor_ok = actor_ok and _bound_proof(classification, declaration, 'denominator_population', {'group_population', 'institutional_population'})

    applicability = {
        'declaration_source_missing': _rule('received_declaration_source', True, 'La déclaration sélectionnée exige sa version originale reçue.', 'Source/version/SHA originale et passages exacts.', new_source=not source_ok),
        'exact_passage_not_found': _rule('exact_declaration_passage', True, 'Une citation de cette déclaration doit appartenir au passage reçu et conserver son contexte.', 'Quote exacte, locator, empreinte et contexte de la même version.'),
        'vote_exact_not_identified': _rule('exact_related_scrutiny', True, 'Le vote mentionné, même déclaré non comparable, doit être un scrutin exact.', 'Identifiant institutionnel original et acte lié sans substitution.', new_source=not vote_source_ok),
        'vote_object_ambiguous': _rule('exact_related_vote_object', True, 'La distinction ou comparaison dépend de l’objet réel et de sa procédure.', 'Objet officiel du scrutin ; retrait et identité de la cible si applicable.', new_source=not vote_source_ok or (is_withdrawn and not declaration['evidence_bindings'])),
        'actor_not_linked_at_relevant_date': _rule('capacity_separation' if is_noncomparable else 'actor_at_event_or_institutional_period', not is_withdrawn, 'La capacité différente est à établir.' if is_noncomparable else ('Relation personnelle/groupe à la date requise par la conclusion.' if not is_withdrawn else 'La conclusion porte sur le retrait d’un objet, sans inventer un vote personnel de cet objet.'), 'Attribution/capacité originale.' if is_noncomparable else 'Position nominative à ce scrutin ; groupe explicitement observé ou bornes institutionnelles si période.', new_source=period_required and not actor_ok),
        'required_vote_text_version_unestablished': _rule('exact_submitted_text', submitted_required, 'La conclusion compare ce texte soumis.' if submitted_required else 'La conclusion documentaire choisie ne prétend pas juger la version soumise d’un texte.', 'Version littérale soumise et lien institutionnel exact ; une fiche procédurale, URL ou adoption ne suffit pas.', new_source=submitted_required and not version_ok),
        'different_authors_as_personal_behavior': _rule('same_person_for_personal_behavior', archetype == 'personal_declaration_personal_vote', 'Une comparaison de comportement personnel exige la même personne.' if archetype == 'personal_declaration_personal_vote' else 'Les personnes, capacités ou objets sont distingués dans cet archétype.', 'Identités institutionnelles exactes des deux rôles.'),
        'too_many_unknowns_for_conclusion': _rule('only_required_unknowns_block', True, 'Toutes les lacunes nécessaires calculées par le serveur bloquent ; les autres restent visibles.', 'Certificats originaux manquants ou modification explicite de la conclusion, avec nouvelle empreinte.'),
        'rights_incompatible_with_render_mode': _rule('current_rights_source_version_usage', True, 'Chaque version utilisée exige une qualification actuelle pour le rendu.', 'Conditions datées et qualifiées source/version/SHA/usage, attribution et expiration.'),
    }
    resolved = {'declaration_source_missing': source_ok, 'exact_passage_not_found': quote_ok,
        'vote_exact_not_identified': vote_ok and vote_source_ok,
        'vote_object_ambiguous': object_ok and withdrawal_ok,
        'actor_not_linked_at_relevant_date': actor_ok,
        'required_vote_text_version_unestablished': version_ok,
        'different_authors_as_personal_behavior': author_ok,
        'too_many_unknowns_for_conclusion': not unresolved}
    checks = []
    check_locations = {
        'declaration_source_missing': ['/public_payload/evidence'],
        'exact_passage_not_found': ['/public_payload/evidence'],
        'vote_exact_not_identified': [vote_prefix + 'scrutin_id'],
        'vote_object_ambiguous': [vote_prefix + 'scrutin_id', vote_prefix + 'official_title', vote_prefix + 'procedure',
            *[b['location'] for b in declaration['evidence_bindings'] if b['predicate'] in {'target_object_identity', 'withdrawal', 'different_object'}]],
        'actor_not_linked_at_relevant_date': [*[f'/public_payload/catalogue/vote_observations/{i}' for i in position_indexes],
            *[f'/public_payload/catalogue/affiliations/{i}' for i in affiliation_indexes],
            *(['/public_payload/actors/compared_actor/person_id'] if personal_vote_required else []),
            *[b['location'] for b in declaration['evidence_bindings'] if b['predicate'] == 'speaker_capacity']],
        'required_vote_text_version_unestablished': [vote_prefix + 'submitted_text_id', '/public_payload/catalogue/text_versions'],
        'different_authors_as_personal_behavior': ['/public_payload/actors'],
    }
    for code, rule in applicability.items():
        if code == 'rights_incompatible_with_render_mode':
            continue  # Qualified at the actual render mode/date by preflight.
        locations = [u['location'] for u in unresolved] if code == 'too_many_unknowns_for_conclusion' else check_locations.get(code, [])
        claims = [u['claim_id'] for u in unresolved] if code == 'too_many_unknowns_for_conclusion' else [c['claim_id'] for c in classification['claims'] if c['claim_id'] in required_ids and any(c['location'] == p or c['location'].startswith(p + '/') for p in locations)]
        checks.append({'code': code, **deepcopy(rule), 'status': 'not_applicable' if not rule['required'] else ('established' if resolved[code] else 'missing'),
            'locations': locations, 'blocking_claim_ids': sorted(set(claims)) if rule['required'] and not resolved[code] else [],
            'absence_proven': False})
    from .verification_counterevidence import AXES
    axis_rules = {
        'contrary_interpretation': ('independent_conclusion_meaning', True),
        'different_vote_same_object_period': ('other_vote_in_claimed_period', period_required),
        'affiliation_change': ('institutional_membership_period', period_required),
        'actor_capacity': ('actors_and_capacities_never_conflated', True),
        'procedural_vote': ('object_and_procedure_never_substituted', True),
        'submitted_vs_adopted_text': ('exact_submitted_text', submitted_required),
        'selection_too_narrow': ('material_limits_always_disclosed', True),
        'quote_out_of_context': ('selected_quote_exact_context', True),
    }
    counter = {axis: {**_rule(axis_rules[axis][0], axis_rules[axis][1],
        'L’axe vérifie la conclusion choisie et ses réserves, sans effacer les alertes hors périmètre.',
        'Recherche adversariale indépendante avec supports exacts, contexte et limites.', model=True),
        'applicable': axis_rules[axis][1], 'all_axes_must_be_reported': True} for axis in AXES}
    findings = {
        'institutional_exhaustiveness_not_established': _rule('institutional_population_or_period', institutional_exhaustiveness,
            'Exhaustivité nécessaire pour la population institutionnelle/période revendiquée.' if institutional_exhaustiveness else 'Le résultat se limite explicitement aux positions reçues ou à cette comparaison individuelle ; la lacune institutionnelle reste visible.',
            'Population institutionnelle définie et collectée pour ce groupe/date/période.'),
        'submitted_version_unestablished': deepcopy(applicability['required_vote_text_version_unestablished']),
        'expected_entity_not_in_selected_support': _rule('selected_support_relevance', True, 'Un fragment officiel non pertinent demeure un contre-témoin ; un passage voisin ne le remplace pas.', 'Recevoir un jugement distinct sur le fragment choisi et conserver l’objection initiale.', model=True),
        'observation_is_not_membership_boundary': _rule('observation_never_membership_boundary', True, 'Une observation ne prouve jamais une entrée/sortie ; cette objection reste conservée.', 'Bornes institutionnelles originales si une période est réellement revendiquée.'),
    }
    routing = [{k: c.get(k) for k in ('claim_id', 'location', 'value_sha256', 'layer', 'fact_type', 'certificate_id', 'provenance_status', 'selection_rule_id')} for c in classification['claims']]
    graph = {'contract': GRAPH_CONTRACT, 'ruleset_version': RULESET,
        'fingerprint': sha256_digest(snapshot), 'conclusion_digest': sha256_digest(declaration),
        'classification_routing_digest': sha256_digest(routing), 'archetype': archetype,
        'submitted_text_binding': submitted_binding,
        'collective_context_projection': collective_projection,
        'declaration': declaration, 'necessities': necessities,
        'required_claim_ids': sorted(required_ids),
        'not_applicable_claim_ids': sorted({n['claim_id'] for n in necessities if not n['necessary_for_conclusion']}),
        'check_applicability': applicability, 'checks': checks,
        'counterevidence_requirements': counter, 'finding_applicability': findings,
        'summary': {'required_claims': len(required_ids),
            'required_unknowns': len(unresolved), 'not_applicable_fields': sum(not n['necessary_for_conclusion'] for n in necessities),
            'documentary_status': 'hold' if any(c['status'] == 'missing' for c in checks) else 'ready_for_independent_verification',
            'independent_meaning_verification_required': True, 'absence_proven': False},
        'limits': ['Graphe de nécessité serveur ; aucun jugement favorable de sens.',
            'Un texte libre B doit correspondre au contrat de conclusion et à ses réserves, sous contrôle indépendant.',
            'not_applicable décrit une dépendance hors conclusion, jamais un PASS ni une absence prouvée.']}
    graph['graph_digest'] = sha256_digest(graph)
    return graph


def validate_dependency_graph(snapshot, classification, report, *, conclusion=None,
                              source_root=None, received_sources=None):
    if not isinstance(report, dict) or report.get('contract') != GRAPH_CONTRACT:
        raise ValidationError('dependency_graph_contract_required')
    from .proof_classification import validate_classification
    validate_classification(snapshot, classification, source_root=source_root, received_sources=received_sources)
    rebuilt = build_dependency_graph(snapshot, classification, conclusion=conclusion)
    if rebuilt != report:
        raise ValidationError('dependency_graph_binding_or_rules_changed')
    return report
