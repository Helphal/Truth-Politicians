"""Versioned proof routing. Reclassification never establishes an external fact.

The default is semantic. Only a recomputed provenance certificate, a narrow
editorial rule or an explicitly unknown value can remove a scalar from B.
"""
from copy import deepcopy
import re

from django.core.exceptions import ValidationError

from .canonical import sha256_digest
from . import verification as legacy

CONTRACT = 'dv.proof-classification/1'
CONTRACT_V2 = 'dv.proof-classification/2'
LAYERS = frozenset({'deterministic', 'semantic', 'editorial', 'unknown'})
UNKNOWN_VALUES = frozenset({'unknown', 'missing', 'ambiguous', 'unestablished',
                            'inconnu', 'inconnue', 'non_etabli', 'non_etablie'})
EDITORIAL_PATHS = (
    r'/public_payload/acts/\d+/included',
    r'/public_payload/themes/\d+/id',
)
SEMANTIC_PATHS = (
    r'/(?:title|short_answer|main_caveat|reasoning|result)',
    r'/public_payload/(?:title|short_answer|main_caveat|reasoning|result|scope|method_explanation|declaration_quote)',
    r'/public_payload/(?:attribution|actors|counterevidence|qualification_material)/.*',
)


def resolve(content, location):
    if not isinstance(location, str) or not location.startswith('/'):
        raise ValidationError('proof_location_required')
    value = content
    try:
        for raw in location[1:].split('/'):
            part = raw.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, ValueError, TypeError):
        raise ValidationError('proof_location_missing') from None
    return value


def _strings(values, code, *, nonempty=False):
    if (not isinstance(values, list) or (nonempty and not values)
            or any(type(x) is not str or not x.strip() for x in values)):
        raise ValidationError(code)
    return values


def _editorial_rule(snapshot, rule):
    fields = {'selection_rule_id', 'version', 'location', 'motif',
              'included_objects', 'excluded_objects', 'limits', 'counter_cases'}
    if not isinstance(rule, dict) or set(rule) != fields:
        raise ValidationError('invalid_editorial_selection_rule')
    if any(type(rule[k]) is not str or not rule[k].strip()
           for k in ('selection_rule_id', 'version', 'location', 'motif')):
        raise ValidationError('editorial_selection_identity_required')
    if not any(re.fullmatch(pattern, rule['location']) for pattern in EDITORIAL_PATHS):
        raise ValidationError('external_fact_cannot_be_editorial_selection')
    value = resolve(snapshot['content'], rule['location'])
    if rule['location'].endswith('/included') and type(value) is not bool:
        raise ValidationError('editorial_inclusion_requires_boolean')
    if re.fullmatch(r'/public_payload/themes/\d+/id', rule['location']):
        theme = resolve(snapshot['content'], rule['location'].rsplit('/', 1)[0])
        if not isinstance(theme, dict) or theme.get('origin') != 'proposed':
            raise ValidationError('editorial_theme_must_be_proposed')
        catalogue = snapshot['content'].get('public_payload', {}).get('catalogue', {})
        family = {'text': 'text_versions', 'dossier': 'legislative_dossiers'}.get(theme.get('target_type'))
        targets = [item for item in catalogue.get(family, []) if isinstance(item, dict)
                   and item.get('id') == theme.get('target_id')] if family else []
        sources = [s['reference'] for s in snapshot.get('sources', []) if s.get('state') == 'available']
        if len(targets) != 1 or targets[0].get('source_version') not in sources:
            raise ValidationError('editorial_theme_target_not_exact_received_object')
    for field in ('included_objects', 'excluded_objects', 'limits', 'counter_cases'):
        _strings(rule[field], 'editorial_selection_scope_required', nonempty=field == 'limits')
    if set(rule['included_objects']) & set(rule['excluded_objects']):
        raise ValidationError('editorial_selection_overlap')
    if rule['location'].endswith('/id') and theme['target_id'] not in rule['included_objects']:
        raise ValidationError('editorial_theme_target_not_in_selection')
    return deepcopy(rule)


def _unknown(snapshot, item, *, v2=False, certificates=None):
    fields = {'location', 'unknown_kind', 'motif', 'limits'}
    if not isinstance(item, dict) or set(item) != fields:
        raise ValidationError('invalid_unknown_annotation')
    if any(type(item[k]) is not str or not item[k].strip()
           for k in ('location', 'unknown_kind', 'motif')):
        raise ValidationError('unknown_annotation_identity_required')
    _strings(item['limits'], 'unknown_limits_required', nonempty=True)
    value = resolve(snapshot['content'], item['location'])
    explicit = value is None or (type(value) is str and value in UNKNOWN_VALUES)
    legacy_bound = (v2 and item['unknown_kind'] == 'historical_bound_not_established'
                    and re.fullmatch(r'/public_payload/catalogue/affiliations/\d+/legacy_bounds/(starts_on|ends_on|observed_on)', item['location']))
    if not explicit and not legacy_bound:
        raise ValidationError('known_external_fact_cannot_be_exempted_as_unknown')
    optional_observation = False
    match = re.fullmatch(r'/public_payload/catalogue/affiliations/(\d+)/(?:legacy_bounds/)?(?:starts_on|ends_on|observed_on)', item['location'])
    if v2 and match:
        optional_observation = _observed_event_proven(snapshot, int(match.group(1)), certificates or {})
    return {**deepcopy(item), 'claim_id': sha256_digest([item['location'], value])[:24],
            'value_sha256': sha256_digest(value), 'represented_value': deepcopy(value),
            'absence_proven': False, 'necessary_for_conclusion': not optional_observation,
            'necessity_basis': 'exact_observation_event_proven_no_membership_period_asserted' if optional_observation else 'unknown_necessary_by_default'}


def _v2_projection(snapshot):
    payload = snapshot['content'].get('public_payload', {})
    catalogue = payload.get('catalogue', {}) if isinstance(payload, dict) else {}
    version = payload.get('public_contract_version') if isinstance(payload, dict) else None
    versions = {'dv.content/0.6.0-provenance-draft.1', 'dv.content/0.7.0-conclusion-draft.1'}
    if version not in versions and catalogue.get('contract') != 'dv.catalogue/2':
        return False
    if version not in versions or catalogue.get('contract') != 'dv.catalogue/2':
        raise ValidationError('proof_projection_requires_explicit_v2_contract')
    from jsonschema import Draft202012Validator, FormatChecker
    from .contracts import publication_schema_for
    schema = publication_schema_for(version)
    kind = {'speech_vote': 'speechVote', 'official_vote': 'officialVote', 'vote_comparison': 'voteComparison'}.get(snapshot.get('kind'))
    if not kind:
        raise ValidationError('proof_projection_content_kind_invalid')
    validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/' + kind}, format_checker=FormatChecker())
    public = deepcopy(payload)
    if kind != 'speechVote':
        public.pop('public_contract_version', None)
    if next(validator.iter_errors(public), None) is not None:
        raise ValidationError('proof_projection_closed_public_contract_invalid')
    return True


def _observed_event_proven(snapshot, index, certificates):
    catalogue = snapshot['content']['public_payload']['catalogue']
    row = catalogue['affiliations'][index]
    if (row.get('temporal_kind') != 'observed_at_scrutiny' or row.get('observed_on_state') != 'known'
            or not row.get('observed_on') or row.get('starts_on') is not None or row.get('ends_on') is not None):
        return False
    def proven(path, fact_type=None):
        cert = certificates.get(path)
        return bool(cert and cert['status'] == 'proven'
                    and (fact_type is None or cert['fact_type'] == fact_type)
                    and cert['result_sha256'] == sha256_digest(resolve(snapshot['content'], path)))
    prefix = f'/public_payload/catalogue/affiliations/{index}/'
    if not all(proven(prefix + field, 'affiliation_observed_on' if field == 'observed_on' else None)
               for field in ('person_id', 'organization_id', 'observed_on')):
        return False
    for vote_index, vote in enumerate(catalogue['vote_observations']):
        if (vote.get('person_id') != row.get('person_id') or vote.get('affiliation_at_event_id') != row.get('organization_id')
                or vote.get('source_version') != row.get('source_version')):
            continue
        vote_prefix = f'/public_payload/catalogue/vote_observations/{vote_index}/'
        if not all(proven(vote_prefix + field) for field in ('person_id', 'affiliation_at_event_id', 'scrutiny_id')):
            continue
        for scrutiny_index, scrutiny in enumerate(catalogue['scrutinies']):
            if (scrutiny.get('scrutin_id') == vote.get('scrutiny_id')
                    and scrutiny.get('occurred_on') == row.get('observed_on')
                    and scrutiny.get('source_version') == row.get('source_version')
                    and all(proven(f'/public_payload/catalogue/scrutinies/{scrutiny_index}/' + field)
                            for field in ('scrutin_id', 'occurred_on'))):
                return True
    return False


def _v2_unknowns(snapshot):
    result = []
    for index, row in enumerate(snapshot['content']['public_payload']['catalogue']['affiliations']):
        prefix = f'/public_payload/catalogue/affiliations/{index}/'
        for field in ('starts_on', 'ends_on', 'observed_on'):
            if row[field] is None or row[field + '_state'] == 'unknown':
                result.append({'location': prefix + field, 'unknown_kind': 'membership_bound_unknown' if field != 'observed_on' else 'observation_date_unknown',
                    'motif': 'Date explicitement inconnue dans la projection serveur catalogue/2.',
                    'limits': ['Aucune période ni absence d’affiliation déduite de cette valeur.']})
            if row['legacy_bounds'][field] is not None:
                result.append({'location': prefix + 'legacy_bounds/' + field, 'unknown_kind': 'historical_bound_not_established',
                    'motif': 'Valeur reçue historique isolée ; aucune borne politique actuelle établie.',
                    'limits': ['Le reçu historique ne vaut ni extraction de période ni jugement de sens.']})
    return result


def _v2_metadata(location):
    return bool(location in {'/public_payload/public_contract_version', '/public_payload/catalogue/contract',
                             '/public_payload/catalogue/projection_ruleset'}
                or re.fullmatch(r'/public_payload/catalogue/affiliations/\d+/(?:starts_on_state|ends_on_state|observed_on_state|temporal_kind|unknowns/\d+|legacy_bounds/(?:historical_only|meaning)|known_at/(?:state|basis))', location))


def _certificates(snapshot, provenance_report, source_root, received_sources):
    if provenance_report is None:
        return {}
    # This call reads the explicitly configured received originals. A digest
    # on a caller-supplied 'proven' certificate is never self-authenticating.
    from . import provenance
    provenance.validate_report(snapshot, provenance_report, source_root=source_root, received_sources=received_sources)
    by_location = {}
    for certificate in provenance_report.get('certificates', []):
        location = certificate.get('location')
        if location in by_location:
            raise ValidationError('duplicate_provenance_certificate_location')
        by_location[location] = certificate
    return by_location


def classify_snapshot(snapshot, *, provenance_report=None, editorial_rules=(),
                      unknowns=(), source_root=None, received_sources=None, conclusion=None):
    declared = snapshot.get('conclusion')
    if conclusion is not None and conclusion != declared:
        raise ValidationError('classification_conclusion_not_in_full_snapshot')
    conclusion = declared
    if (snapshot.get('content', {}).get('public_payload', {}).get('public_contract_version')
            == 'dv.content/0.7.0-conclusion-draft.1' and not conclusion):
        raise ValidationError('classification_conclusion_required_for_public_profile')
    originals = list(legacy._scalars(snapshot['content']))
    if snapshot.get('claims') != originals:
        raise ValidationError('classification_snapshot_claims_changed')
    fingerprint = sha256_digest(snapshot)
    rules = [_editorial_rule(snapshot, r) for r in editorial_rules]
    v2 = _v2_projection(snapshot)
    certificates = _certificates(snapshot, provenance_report, source_root, received_sources)
    annotations = [_unknown(snapshot, u, v2=v2, certificates=certificates) for u in unknowns]
    if (len({r['location'] for r in rules}) != len(rules)
            or len({u['location'] for u in annotations}) != len(annotations)):
        raise ValidationError('duplicate_proof_routing_location')
    rule_map = {r['location']: r for r in rules}
    if v2:
        generated = [_unknown(snapshot, u, v2=True, certificates=certificates) for u in _v2_unknowns(snapshot)]
        generated_locations = {u['location'] for u in generated}
        annotations = [u for u in annotations if u['location'] not in generated_locations] + generated
    unknown_map = {u['location']: u for u in annotations}
    routed = []
    for original in originals:
        location = original['location']
        record = {'claim_id': original['id'], 'location': location,
                  'value_sha256': sha256_digest(original['value']), 'layer': 'semantic',
                  'fact_type': 'meaning_requires_independent_verification',
                  'certificate_id': None, 'selection_rule_id': None,
                  'necessary_for_conclusion': True,
                  'reasons': ['Aucune exemption de sens déduite du nom du champ.']}
        if legacy.structural_metadata(original, snapshot) or (v2 and _v2_metadata(location)):
            record.update(layer='deterministic', fact_type='projection_binding',
                necessary_for_conclusion=False, certificate_id='structural-' + sha256_digest([fingerprint, original]),
                provenance_status='proven', reasons=['Structure reçue et empreinte exacte, aucun fait politique déduit.'])
        elif location in rule_map:
            rule = rule_map[location]
            record.update(layer='editorial', fact_type='editorial_selection',
                selection_rule_id=rule['selection_rule_id'], necessary_for_conclusion=False,
                reasons=[rule['motif'], *rule['limits']])
        elif location in unknown_map:
            annotation = unknown_map[location]
            record.update(layer='unknown', fact_type=annotation['unknown_kind'],
                necessary_for_conclusion=annotation['necessary_for_conclusion'],
                reasons=[annotation['motif'], *annotation['limits']])
        elif location in certificates and (not any(re.fullmatch(pattern, location) for pattern in SEMANTIC_PATHS)
                or (location in {'/public_payload/actors/speaker/capacity', '/public_payload/attribution/capacity'}
                    and certificates[location].get('fact_type') == 'declared_actor_capacity')):
            certificate = certificates[location]
            fact_type = certificate.get('fact_type', '')
            # An observation is not an entry/exit. Even a mechanically valid
            # extraction certificate cannot change the proposition asserted.
            bound = re.fullmatch(r'/public_payload/catalogue/affiliations/\d+/(starts_on|ends_on|affiliation_starts_on|affiliation_ends_on)', location)
            correct_bound_type = (not bound or (bound.group(1) in {'starts_on', 'affiliation_starts_on'} and fact_type in {'affiliation_start', 'membership_start_date'})
                                  or (bound.group(1) in {'ends_on', 'affiliation_ends_on'} and fact_type in {'affiliation_end', 'membership_end_date'}))
            if correct_bound_type and certificate.get('claim_id') == original['id']:
                if certificate.get('status') == 'proven' and certificate.get('result_sha256') != sha256_digest(original['value']):
                    raise ValidationError('proven_certificate_value_mismatch')
                record.update(layer='deterministic', fact_type=fact_type,
                    certificate_id=certificate['certificate_id'], provenance_status=certificate['status'],
                    reasons=deepcopy(certificate.get('reasons', [])) or ['Certificat recomputé depuis l’original reçu.'])
            elif not correct_bound_type:
                record['reasons'] = ['Date d’observation/scrutin insuffisante pour établir une entrée ou sortie d’affiliation.']
        routed.append(record)
    report = {'contract': CONTRACT, 'fingerprint': fingerprint,
              'claims': routed, 'unknowns': annotations, 'editorial_rules': rules,
              'provenance_report': deepcopy(provenance_report),
              'summary': {layer: sum(c['layer'] == layer for c in routed) for layer in sorted(LAYERS)}}
    report['summary']['explicit_unknown_fields'] = len(annotations)
    if conclusion:
        from . import conclusion_dependencies as dependencies
        graph = dependencies.build_dependency_graph(snapshot, report, conclusion=conclusion)
        necessities = {n['location']: n for n in graph['necessities']}
        for record in [*report['claims'], *report['unknowns']]:
            necessity = necessities[record['location']]
            record['necessary_for_conclusion'] = necessity['necessary_for_conclusion']
            record['dependency_rule_id'] = necessity['dependency_rule_id']
            record['applicability'] = necessity['applicability']
        report.update(contract=CONTRACT_V2, conclusion=deepcopy(conclusion), dependency_graph=graph)
        report['summary']['required_by_layer'] = {
            layer: sum(c['layer'] == layer and c['necessary_for_conclusion'] for c in routed)
            for layer in sorted(LAYERS)}
        report['summary']['not_applicable_fields'] = graph['summary']['not_applicable_fields']
    report['classification_digest'] = sha256_digest(report)
    return report


def validate_classification(snapshot, report, *, source_root=None, received_sources=None):
    expected_contract = CONTRACT_V2 if snapshot.get('conclusion') else CONTRACT
    if not isinstance(report, dict) or report.get('contract') != expected_contract:
        raise ValidationError('classification_contract_required')
    rebuilt = classify_snapshot(snapshot, provenance_report=report.get('provenance_report'),
        editorial_rules=report.get('editorial_rules', []),
        unknowns=[{k: item[k] for k in ('location', 'unknown_kind', 'motif', 'limits')}
                  for item in report.get('unknowns', [])], source_root=source_root, received_sources=received_sources)
    if rebuilt != report:
        raise ValidationError('classification_binding_or_routing_changed')
    return report
