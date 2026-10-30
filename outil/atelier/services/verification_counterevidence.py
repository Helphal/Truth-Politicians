"""DV-CONTREPREUVE: durable bounded adversarial checks, never a truth oracle.

The local scan operates on received passages and exact first-pass citations.
It can raise an objection. No local absence, string match or JSON actor label
can establish semantic support or a genuinely independent second judgment.
"""
from copy import deepcopy
import re
import unicodedata

from django.core.exceptions import ValidationError

from .canonical import sha256_digest
from .canonical import canonical_bytes

CONTRACT = 'dv.counterevidence/1'
DEPENDENCY_CONTRACT = 'dv.counterevidence/2'
AXES = (
    'contrary_interpretation', 'different_vote_same_object_period',
    'affiliation_change', 'actor_capacity', 'procedural_vote',
    'submitted_vs_adopted_text', 'selection_too_narrow', 'quote_out_of_context',
)
ADVERSARIAL_PLAN = 'dv.adversarial-plan/1'
ADVERSARIAL_RESPONSE = 'dv.adversarial-response/1'
ADVERSARIAL_PLAN_V2 = 'dv.adversarial-plan/2'
ADVERSARIAL_RESPONSE_V2 = 'dv.adversarial-response/2'
ADVERSARIAL_RESPONSE_V3 = 'dv.adversarial-response/3'
SCHEMA_PROFILE_V4 = 'dv.adversarial-schema/4'
_DEFAULT_MAX_CLAIMS = object()
MAX_ADVERSARIAL_INPUT = 4 * 1024 * 1024
MAX_ADVERSARIAL_OUTPUT = 128 * 1024


def _normal(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c)).replace('’', "'")


def _passages(snapshot):
    result = {}
    for source in snapshot.get('sources', []):
        for passage in source.get('passages', []):
            key = str(passage['id'])
            if key in result:
                raise ValidationError('counterevidence_duplicate_passage')
            result[key] = {'source': source.get('reference'), 'passage': passage}
    return result


def _parent(content, location):
    from .proof_classification import resolve
    try:
        value = resolve(content, location.rsplit('/', 1)[0])
    except ValidationError:
        return {}
    return value if isinstance(value, dict) else {}


def _entities(snapshot, claim):
    owner = _parent(snapshot['content'], claim['location'])
    catalogue = snapshot['content'].get('public_payload', {}).get('catalogue', {})
    values = []
    person_id, organization_id = owner.get('person_id'), owner.get('organization_id')
    for person in catalogue.get('persons', []):
        if person.get('id') == person_id and person.get('display_name'):
            values.append(person['display_name'])
    for organization in catalogue.get('organizations', []):
        if organization.get('id') == organization_id:
            for key in ('name', 'label', 'short_name', 'abbreviation'):
                if organization.get(key):
                    values.append(organization[key])
            if type(organization_id) is str and ':' in organization_id:
                alias = organization_id.rsplit(':', 1)[-1]
                if len(alias) > 1 and not alias.isdigit():
                    values.append(alias)
    return sorted(set(values))


def _finding(axis, code, reason, *, claim=None, proof=None, severity='risk'):
    value = {'axis': axis, 'code': code, 'severity': severity,
             'claim_id': claim.get('id') if claim else None,
             'location': claim.get('location') if claim else None,
             'reason': reason, 'proof': deepcopy(proof),
             'requires_independent_semantic_review': True}
    value['finding_id'] = 'counter-' + sha256_digest(value)
    return value


def _proof(entry, quote, *, before='', after=''):
    passage = entry['passage']
    return {'source_version': deepcopy(entry['source']), 'passage_id': passage['id'],
            'locator': passage['locator'], 'quote': quote, 'quote_sha256': sha256_digest(quote),
            'context_before': before, 'context_after': after,
            'passage_sha256': sha256_digest(passage['exact_text'])}


def _validate_first(snapshot, first_result):
    if first_result is None:
        return []
    if not isinstance(first_result, dict) or first_result.get('fingerprint') != sha256_digest(snapshot):
        raise ValidationError('counterevidence_first_judgment_fingerprint_mismatch')
    originals = {c['id']: c for c in snapshot['claims']}
    claims = first_result.get('claims')
    if not isinstance(claims, list):
        raise ValidationError('counterevidence_first_judgment_claims_required')
    seen = set()
    for claim in claims:
        original = originals.get(claim.get('id')) if isinstance(claim, dict) else None
        if (not original or claim['id'] in seen or any(claim.get(k) != original[k] for k in ('location', 'value'))):
            raise ValidationError('counterevidence_foreign_or_duplicate_claim')
        seen.add(claim['id'])
        if any(not isinstance(claim.get(family), list) for family in ('support', 'against')):
            raise ValidationError('counterevidence_first_judgment_evidence_required')
    return claims


def scan_counterevidence(snapshot, first_result=None, *, generation_actor_id=None,
                         first_judgment_actor_id=None, first_judgment_execution_id=None,
                         counter_actor_id=None, counter_execution_id=None):
    passages = _passages(snapshot)
    claims = _validate_first(snapshot, first_result)
    findings = []
    for claim in claims:
        supports = []
        for family in ('support', 'against'):
            for reference in claim[family]:
                if not isinstance(reference, dict) or set(reference) != {'passage_id', 'quote'}:
                    raise ValidationError('counterevidence_invalid_passage_reference')
                entry = passages.get(str(reference['passage_id']))
                quote = reference['quote']
                if (entry is None or type(quote) is not str or not quote.strip()
                        or quote not in entry['passage']['exact_text']):
                    raise ValidationError('counterevidence_quote_not_in_received_source')
                text = entry['passage']['exact_text']
                offset = text.find(quote)
                before, after = text[max(0, offset - 100):offset], text[offset + len(quote):offset + len(quote) + 100]
                proof = _proof(entry, quote, before=before, after=after)
                if family == 'against':
                    findings.append(_finding('contrary_interpretation', 'received_counterevidence',
                        'Contre-élément déjà reçu, sans suppression ni équilibrage artificiel.', claim=claim, proof=proof, severity='objection'))
                    continue
                supports.append((entry, quote))
                outside = _normal(before + ' ' + after)
                inside = _normal(quote)
                negation = r"\b(?:pas|jamais|aucun|aucune|non|sauf)\b|\bne\b|\bn[' ]"
                condition = r'\bsi\b|sous reserve|a condition|pour autant que'
                if ((re.search(negation, outside) and not re.search(negation, inside))
                        or (re.search(condition, outside) and not re.search(condition, inside))):
                    findings.append(_finding('quote_out_of_context', 'negation_or_condition_outside_quote',
                        'Une négation/condition apparaît dans le contexte omis. Sens à recevoir séparément.', claim=claim, proof=proof))
                if len(inside.split()) <= 8 and not any(word in inside for word in ('vote', 'voter', 'adopte', 'rejete', 'pour', 'contre')):
                    findings.append(_finding('quote_out_of_context', 'context_only_evidence',
                        'Support bref de contexte/identité ; il n’établit pas automatiquement une conclusion.', claim=claim, proof=proof))
        expected = _entities(snapshot, claim)
        if expected and supports:
            for entry, quote in supports:
                if not any(_normal(entity) in _normal(quote) for entity in expected):
                    findings.append(_finding('actor_capacity', 'expected_entity_not_in_selected_support',
                        'Aucun nom/groupe attendu n’apparaît dans ce fragment choisi. Un autre support ne répare pas sa pertinence ; contrôle de contexte requis.',
                        claim=claim, proof={'expected_entities': expected,
                            'selected_passages': [_proof(entry, quote)]}))
        if re.fullmatch(r'/public_payload/catalogue/affiliations/\d+/(starts_on|ends_on)', claim['location']):
            owner = _parent(snapshot['content'], claim['location'])
            if owner.get('observed_on') == claim['value'] or owner.get('status') in {'membership_attested_at_scrutiny', 'observed'}:
                findings.append(_finding('affiliation_change', 'observation_is_not_membership_boundary',
                    'La date d’observation/scrutin ne prouve pas une entrée ou une sortie du groupe.', claim=claim,
                    proof={'observed_on': owner.get('observed_on'), 'claimed_bound': claim['value'],
                           'source_version': deepcopy(owner.get('source_version'))}, severity='objection'))
    payload = snapshot['content'].get('public_payload', {})
    actors = payload.get('actors', {}) if isinstance(payload.get('actors', {}), dict) else {}
    narrative = _normal(' '.join(str(snapshot['content'].get(k, '')) for k in ('short_answer', 'reasoning', 'result')))
    compared_scope = payload.get('compared_actor_scope')
    if actors.get('scope') == 'government' and (compared_scope == 'personal' or payload.get('comparison_mode') == 'personal_behavior'):
        findings.append(_finding('actor_capacity', 'government_is_not_minister_personal_vote',
            'Une capacité gouvernementale ne fournit pas un vote personnel du ministre.',
            proof={'actors': deepcopy(actors), 'compared_actor_scope': compared_scope}, severity='objection'))
    organizations = payload.get('catalogue', {}).get('organizations', [])
    represented = actors.get('represented_entity', {}) or {}
    represented_id = represented.get('organization_id')
    represented_org = next((o for o in organizations if o.get('id') == represented_id), {})
    if represented_org.get('kind') in {'parliamentary_group', 'group'} and (
            actors.get('scope') == 'party' or represented.get('kind') == 'party'):
        findings.append(_finding('actor_capacity', 'parliamentary_group_is_not_party',
            'Le groupe parlementaire reçu est présenté comme parti sans preuve correspondante.',
            proof={'represented_entity': deepcopy(represented), 'organization': deepcopy(represented_org)}, severity='objection'))
    distribution = payload.get('qualification_material', {}).get('collective_vote_distribution', {})
    if re.search(r'\bunanim', narrative) and actors.get('scope') == 'collective':
        findings.append(_finding('actor_capacity', 'collective_group_does_not_prove_unanimity',
            'Une annonce collective n’établit pas l’unanimité ; conserver ventilation et positions inconnues.',
            proof={'distribution': deepcopy(distribution), 'actors': deepcopy(actors)}))
    for vote in payload.get('catalogue', {}).get('scrutinies', []):
        object_text = _normal(' '.join(str(vote.get(k, '')) for k in ('procedure', 'official_title', 'object_type')))
        procedural = any(word in object_text for word in ('motion', 'amendement', 'amendment', 'article', 'suppression'))
        if procedural and payload.get('conclusion_object_scope') in {'whole_text', 'adopted_text'}:
            findings.append(_finding('procedural_vote', 'procedural_vote_is_not_whole_text',
                'Motion/amendement/article et vote sur le texte entier sont des objets distincts.',
                proof={'scrutiny': deepcopy(vote), 'conclusion_object_scope': payload['conclusion_object_scope']}, severity='objection'))
        if vote.get('text_reference_role') != 'submitted':
            findings.append(_finding('submitted_vs_adopted_text', 'submitted_version_unestablished',
                'Version soumise non établie ; aucune identité avec le texte adopté déduite.',
                proof={'scrutiny': deepcopy(vote)}))
    coverage = payload.get('coverage', {})
    if isinstance(coverage, dict) and coverage.get('expected_state') == 'unknown':
        findings.append(_finding('selection_too_narrow', 'institutional_exhaustiveness_not_established',
            'Périmètre matérialisé seulement ; absence dans la collecte ≠ absence institutionnelle.', proof=deepcopy(coverage)))
    seen = set()
    findings = [f for f in findings if not (f['finding_id'] in seen or seen.add(f['finding_id']))]
    known_ids = all(type(x) is int and x > 0 for x in (generation_actor_id, first_judgment_actor_id, counter_actor_id))
    separated = (known_ids and len({generation_actor_id, first_judgment_actor_id, counter_actor_id}) == 3
                 and type(first_judgment_execution_id) is str and bool(first_judgment_execution_id.strip())
                 and type(counter_execution_id) is str and bool(counter_execution_id.strip())
                 and first_judgment_execution_id != counter_execution_id)
    report = {'contract': CONTRACT, 'fingerprint': sha256_digest(snapshot),
              'first_result_digest': sha256_digest(first_result) if first_result is not None else None,
              'origin': 'local_bounded_scan', 'findings': findings,
              'axes': [{'axis': axis, 'status': 'risk_found' if any(f['axis'] == axis for f in findings) else 'not_assessed_semantically',
                        'scope': 'received_material_only', 'absence_proven': False} for axis in AXES],
              'identity': {'generation_actor_id': generation_actor_id, 'first_judgment_actor_id': first_judgment_actor_id,
                           'first_judgment_execution_id': first_judgment_execution_id,
                           'counter_actor_id': counter_actor_id, 'counter_execution_id': counter_execution_id,
                           'declared_separation': separated, 'runtime_identity_verified': False},
              'summary': {'status': 'flagged' if findings else 'no_findings_in_received_material',
                          'finding_count': len(findings), 'counterevidence_complete': False,
                          'independent_semantic_review_required': True,
                          'no_result_is_not_absence_proof': True},
              'limitations': ['Scan local borné : une alerte est une objection, aucun résultat favorable n’est déduit.',
                              'Identités et exécution indépendante à recevoir depuis le runtime, jamais depuis ces chaînes JSON.',
                              'Aucune inférence, recherche externe exhaustive ni validation personnelle.']}
    if snapshot.get('conclusion'):
        from .proof_classification import classify_snapshot
        graph = classify_snapshot(snapshot)['dependency_graph']
        report['contract'] = DEPENDENCY_CONTRACT
        report['dependency_graph_digest'] = graph['graph_digest']
        for finding in report['findings']:
            rule = deepcopy(graph['finding_applicability'].get(finding['code'], {
                'required': True, 'dependency_rule_id': 'dv.dependency-rules/always-retain-adverse-context',
                'why_required_for_this_conclusion': 'Toute objection de citation, sens, capacité ou absence interprétée reste examinée.',
                'what_evidence_would_resolve_it': 'Jugement adverse indépendant sur cette pièce exacte et son contexte.',
                'can_resolve_without_model': False, 'can_resolve_without_new_source': True}))
            finding['conclusion_applicability'] = rule
            finding['applicability'] = 'required' if rule['required'] else 'not_applicable'
        for axis in report['axes']:
            axis['conclusion_applicability'] = deepcopy(graph['counterevidence_requirements'][axis['axis']])
    report['counterevidence_digest'] = sha256_digest(report)
    return report


def validate_counterevidence(snapshot, report, first_result=None):
    expected_contract = DEPENDENCY_CONTRACT if snapshot.get('conclusion') else CONTRACT
    if not isinstance(report, dict) or report.get('contract') != expected_contract:
        raise ValidationError('counterevidence_contract_required')
    identity = report.get('identity', {})
    rebuilt = scan_counterevidence(snapshot, first_result,
        **{k: identity.get(k) for k in ('generation_actor_id', 'first_judgment_actor_id',
                                      'first_judgment_execution_id', 'counter_actor_id', 'counter_execution_id')})
    if rebuilt != report:
        raise ValidationError('counterevidence_binding_or_findings_changed')
    return report


def build_adversarial_plan(snapshot, classification, first_result, *, revision_id,
                           model, effort, max_claims=_DEFAULT_MAX_CLAIMS, resume_margin_phases=2,
                           source_root=None, received_sources=None, job_id=None, job_key=None,
                           owner_id=None, first_receipt_id=None, response_contract=None, schema_profile=None):
    """Local exact plan only. No actor, allocation or model call is invented."""
    from .proof_classification import validate_classification
    validate_classification(snapshot, classification, source_root=source_root, received_sources=received_sources)
    if response_contract not in (None, ADVERSARIAL_RESPONSE_V3):
        raise ValidationError('invalid_adversarial_response_contract')
    if (schema_profile not in (None, SCHEMA_PROFILE_V4)
            or (schema_profile is not None and response_contract != ADVERSARIAL_RESPONSE_V3)):
        raise ValidationError('invalid_adversarial_schema_profile')
    maximum = 10 if schema_profile == SCHEMA_PROFILE_V4 else 8
    if max_claims is _DEFAULT_MAX_CLAIMS:
        max_claims = maximum
    first = _validate_first(snapshot, first_result)
    wanted = [c['claim_id'] for c in classification['claims'] if c['layer'] == 'semantic'
              and c.get('necessary_for_conclusion', True)]
    if {c['id'] for c in first} != set(wanted) or len(first) != len(wanted):
        raise ValidationError('adversarial_first_semantic_coverage_incomplete')
    if (type(max_claims) is not int or not 1 <= max_claims <= maximum
            or type(resume_margin_phases) is not int or not 0 <= resume_margin_phases <= 64
            or type(revision_id) is not int or revision_id < 1
            or type(model) is not str or not model.strip() or type(effort) is not str or not effort.strip()):
        raise ValidationError('invalid_adversarial_plan_bounds')
    passages = _passages(snapshot)
    plan = {'contract': ADVERSARIAL_PLAN_V2 if snapshot.get('conclusion') else ADVERSARIAL_PLAN, 'fingerprint': sha256_digest(snapshot),
            'revision_id': revision_id, 'classification_digest': classification['classification_digest'],
            'classification': deepcopy(classification), 'job_id': job_id, 'job_key': job_key,
            'owner_id': owner_id, 'first_receipt_id': first_receipt_id,
            'model': model, 'effort': effort, 'snapshot': deepcopy(snapshot),
            'first_result': deepcopy(first_result), 'first_result_digest': sha256_digest(first_result),
            'semantic_claim_ids': wanted, 'axes': list(AXES),
            'max_claims': max_claims, 'max_input_bytes': MAX_ADVERSARIAL_INPUT,
            'max_output_bytes': MAX_ADVERSARIAL_OUTPUT, 'batches': [],
            'separation_requirements': ['known_runtime_generation_actor', 'counter_actor_distinct_from_generation',
                'counter_actor_distinct_from_first_judgment', 'counter_execution_distinct_from_first_judgment',
                'current_verifier_role', 'native_response_and_phase_binding'],
            'authorization': 'NOT_AUTHORIZED'}
    if response_contract is not None:
        plan['response_contract'] = response_contract
    if schema_profile is not None:
        plan['schema_profile'] = schema_profile
    by_id = {c['id']: c for c in first}
    for start in range(0, len(wanted), max_claims):
        ids = wanted[start:start + max_claims]
        packet = {'contract': plan['contract'], 'fingerprint': plan['fingerprint'],
            'batch_id': f'adversarial-{start // max_claims:04d}',
            'claims': [deepcopy(by_id[cid]) for cid in ids], 'axes': list(AXES),
            'sources': deepcopy(snapshot['sources']),
            'global_context': {k: deepcopy(snapshot['content'].get(k)) for k in ('short_answer', 'main_caveat', 'reasoning', 'result')},
            'scope': 'received_material_only; no_findings_is_not_proof_of_absence'}
        if response_contract is not None:
            packet['response_contract'] = response_contract
            packet['support_references'] = {cid: {str(i): sha256_digest(support)
                for i, support in enumerate(by_id[cid]['support'])} for cid in ids}
        if schema_profile is not None:
            packet['schema_profile'] = schema_profile
        if snapshot.get('conclusion'):
            packet['conclusion_contract'] = deepcopy(snapshot['conclusion'])
            packet['conclusion_dependencies'] = deepcopy(classification['dependency_graph']['counterevidence_requirements'])
        size = len(canonical_bytes(packet))
        if size > MAX_ADVERSARIAL_INPUT:
            raise ValidationError('adversarial_input_bound_exceeded_without_truncation')
        plan['batches'].append({'batch_id': packet['batch_id'], 'claim_ids': ids,
                               'searched_passage_ids': [int(pid) for pid in passages],
                               'packet': packet, 'packet_digest': sha256_digest(packet), 'input_bytes': size})
    count = len(plan['batches'])
    plan['allocation_proposal'] = {'authorization': 'NOT_AUTHORIZED', 'minimum_new_phases': count,
        'proposed_ceiling': count + resume_margin_phases if count else 0,
        'resume_margin_phases': resume_margin_phases, 'actual_phases_reserved': 0, 'actual_phases_launched': 0}
    plan['plan_digest'] = sha256_digest(plan)
    return plan


def validate_adversarial_plan(plan, classification, *, source_root=None, received_sources=None):
    expected_contract = ADVERSARIAL_PLAN_V2 if isinstance(plan, dict) and plan.get('snapshot', {}).get('conclusion') else ADVERSARIAL_PLAN
    if not isinstance(plan, dict) or plan.get('contract') != expected_contract:
        raise ValidationError('adversarial_plan_contract_required')
    _schema_profile(plan)
    for batch in plan.get('batches', []):
        _schema_profile(plan, batch)
    rebuilt = build_adversarial_plan(plan['snapshot'], classification, plan['first_result'],
        revision_id=plan['revision_id'], model=plan['model'], effort=plan['effort'],
        max_claims=plan['max_claims'], resume_margin_phases=plan['allocation_proposal']['resume_margin_phases'],
        source_root=source_root, received_sources=received_sources, job_id=plan.get('job_id'), job_key=plan.get('job_key'),
        owner_id=plan.get('owner_id'), first_receipt_id=plan.get('first_receipt_id'),
        response_contract=plan.get('response_contract'), schema_profile=plan.get('schema_profile'))
    if rebuilt != plan:
        raise ValidationError('adversarial_plan_content_changed')
    return plan


def _schema_profile(plan, batch=None):
    """The optional schema selector is immutable and never inferred from output."""
    if 'schema_profile' not in plan:
        if batch is not None and 'schema_profile' in batch.get('packet', {}):
            raise ValidationError('adversarial_schema_profile_mismatch')
        return None
    selected = plan['schema_profile']
    if (selected != SCHEMA_PROFILE_V4
            or plan.get('response_contract') != ADVERSARIAL_RESPONSE_V3
            or (batch is not None and batch.get('packet', {}).get('schema_profile') != selected)):
        raise ValidationError('adversarial_schema_profile_mismatch')
    return selected


def _bounded_reason(value, code):
    if type(value) is not str or not value.strip() or len(value) > 4000:
        raise ValidationError(code)


def _validate_legacy_adversarial_response(plan, batch, raw, *, _check_wire_bound=True):
    """Closed native-result contract; authenticity is a separate runtime gate."""
    fields = {'contract', 'fingerprint', 'batch_id', 'claims', 'axes', 'limitations'}
    dependency_aware = bool(plan['snapshot'].get('conclusion'))
    if (not isinstance(raw, dict) or set(raw) != fields or raw['contract'] != (ADVERSARIAL_RESPONSE_V2 if dependency_aware else ADVERSARIAL_RESPONSE)
            or raw['fingerprint'] != plan['fingerprint'] or raw['batch_id'] != batch['batch_id']
            or (_check_wire_bound and len(canonical_bytes(raw)) > plan['max_output_bytes'])):
        raise ValidationError('adversarial_response_binding_or_bound_changed')
    if (not isinstance(raw['limitations'], list) or len(raw['limitations']) > 32):
        raise ValidationError('adversarial_limits_required')
    for limitation in raw['limitations']:
        _bounded_reason(limitation, 'adversarial_limits_required')
    originals = {c['id']: c for c in plan['first_result']['claims']}
    passages = _passages(plan['snapshot'])
    judgments = raw['claims']
    if not isinstance(judgments, list) or len(judgments) != len(batch['claim_ids']):
        raise ValidationError('adversarial_claim_coverage_incomplete')
    seen = set()
    for judgment in judgments:
        if not isinstance(judgment, dict) or set(judgment) != {'claim_id', 'conclusion', 'reason', 'support_relations'}:
            raise ValidationError('invalid_adversarial_claim')
        cid = judgment['claim_id']
        if type(cid) is not str or cid not in batch['claim_ids'] or cid in seen:
            raise ValidationError('adversarial_claim_identity_changed')
        seen.add(cid)
        if judgment['conclusion'] not in {'no_objection', 'qualified', 'contradicted', 'indeterminate'}:
            raise ValidationError('invalid_adversarial_conclusion')
        _bounded_reason(judgment['reason'], 'adversarial_reason_required')
        relations = judgment['support_relations']
        supports = originals[cid]['support']
        if not isinstance(relations, list) or len(relations) != len(supports):
            raise ValidationError('adversarial_selected_support_not_examined')
        indexes = set()
        for relation in relations:
            if not isinstance(relation, dict) or set(relation) != {'support_index', 'relation', 'reason', 'passage_id', 'quote'}:
                raise ValidationError('invalid_adversarial_support_relation')
            index = relation['support_index']
            if type(index) is not int or index < 0 or index >= len(supports) or index in indexes:
                raise ValidationError('adversarial_support_index_changed')
            indexes.add(index)
            if (relation['relation'] not in {'supports', 'context_only', 'irrelevant', 'contradicts', 'indeterminate'}
                    or relation['passage_id'] != supports[index]['passage_id'] or relation['quote'] != supports[index]['quote']):
                raise ValidationError('adversarial_support_replaced_or_relation_invalid')
            _bounded_reason(relation['reason'], 'adversarial_relation_reason_required')
            entry = passages.get(str(relation['passage_id']))
            if not entry or type(relation['quote']) is not str or relation['quote'] not in entry['passage']['exact_text']:
                raise ValidationError('adversarial_support_not_in_received_source')
        if judgment['conclusion'] == 'no_objection' and any(r['relation'] != 'supports' for r in relations):
            raise ValidationError('adversarial_unfavorable_relation_cannot_be_promoted')
    axes = raw['axes']
    if not isinstance(axes, list) or len(axes) != len(AXES):
        raise ValidationError('adversarial_eight_axes_required')
    seen_axes = set()
    for axis in axes:
        if not isinstance(axis, dict) or set(axis) != {'axis', 'status', 'reason', 'searched_passage_ids', 'findings'}:
            raise ValidationError('invalid_adversarial_axis')
        if axis['axis'] not in AXES or axis['axis'] in seen_axes:
            raise ValidationError('adversarial_axis_identity_changed')
        seen_axes.add(axis['axis'])
        statuses = {'found', 'not_found', 'unavailable', 'not_applicable'} if dependency_aware else {'found', 'not_found', 'unavailable'}
        if axis['status'] not in statuses:
            raise ValidationError('invalid_adversarial_axis_status')
        if dependency_aware:
            applicable = plan['classification']['dependency_graph']['counterevidence_requirements'][axis['axis']]['required']
            if axis['status'] == 'not_applicable' and applicable:
                raise ValidationError('necessary_adversarial_axis_cannot_be_downgraded')
            if not applicable and axis['status'] == 'not_found':
                raise ValidationError('unrequired_adversarial_axis_requires_explicit_not_applicable')
        _bounded_reason(axis['reason'], 'adversarial_axis_reason_required')
        searched = axis['searched_passage_ids']
        if (not isinstance(searched, list) or any(type(pid) is not int for pid in searched)
                or len(searched) != len(set(searched))
                or (axis['status'] != 'unavailable' and set(searched) != set(batch['searched_passage_ids']))):
            raise ValidationError('adversarial_received_material_scope_not_covered')
        if not isinstance(axis['findings'], list) or len(axis['findings']) > 32 or (axis['status'] == 'found') != bool(axis['findings']):
            raise ValidationError('adversarial_axis_findings_inconsistent')
        for finding in axis['findings']:
            if not isinstance(finding, dict) or set(finding) != {'claim_id', 'passage_id', 'quote', 'relation', 'reason'}:
                raise ValidationError('invalid_adversarial_finding')
            entry = passages.get(str(finding['passage_id']))
            if (finding['claim_id'] not in batch['claim_ids'] or entry is None
                    or type(finding['quote']) is not str or not finding['quote'].strip() or len(finding['quote']) > 4096
                    or finding['quote'] not in entry['passage']['exact_text']
                    or finding['relation'] not in {'contradicts', 'qualifies', 'context_only', 'irrelevant'}):
                raise ValidationError('adversarial_finding_not_bound_to_received_material')
            _bounded_reason(finding['reason'], 'adversarial_finding_reason_required')
    return raw


def _expand_v3_response(plan, batch, raw):
    """Resolve references from the immutable first result; never alter raw data."""
    fields = {'contract', 'fingerprint', 'batch_id', 'claims', 'axes', 'limitations'}
    if (not isinstance(raw, dict) or set(raw) != fields or raw.get('contract') != ADVERSARIAL_RESPONSE_V3
            or raw.get('fingerprint') != plan['fingerprint'] or raw.get('batch_id') != batch['batch_id']
            or len(canonical_bytes(raw)) > plan['max_output_bytes']):
        raise ValidationError('adversarial_response_binding_or_bound_changed')
    if batch.get('packet', {}).get('response_contract') != ADVERSARIAL_RESPONSE_V3:
        raise ValidationError('adversarial_response_contract_mismatch')
    originals = {c['id']: c for c in plan['first_result']['claims']}
    references = {cid: {str(i): sha256_digest(support) for i, support in enumerate(originals[cid]['support'])}
                  for cid in batch['claim_ids']}
    if batch['packet'].get('support_references') != references:
        raise ValidationError('adversarial_support_reference_plan_changed')
    if not isinstance(raw['claims'], dict) or set(raw['claims']) != set(batch['claim_ids']):
        raise ValidationError('adversarial_claim_coverage_incomplete')
    if not isinstance(raw['axes'], dict) or set(raw['axes']) != set(AXES):
        raise ValidationError('adversarial_eight_axes_required')
    expanded = {'contract': ADVERSARIAL_RESPONSE_V2 if plan['snapshot'].get('conclusion') else ADVERSARIAL_RESPONSE,
        'fingerprint': raw['fingerprint'], 'batch_id': raw['batch_id'], 'claims': [], 'axes': [],
        'limitations': deepcopy(raw['limitations'])}
    for cid in batch['claim_ids']:
        judgment = raw['claims'][cid]
        if not isinstance(judgment, dict) or set(judgment) != {'conclusion', 'reason', 'support_relations'}:
            raise ValidationError('invalid_adversarial_claim')
        relations = judgment['support_relations']
        supports = originals[cid]['support']
        if not isinstance(relations, dict) or len(relations) != len(supports):
            raise ValidationError('adversarial_selected_support_not_examined')
        if set(relations) != {str(i) for i in range(len(supports))}:
            raise ValidationError('adversarial_support_index_changed')
        item = {'claim_id': cid, 'conclusion': judgment['conclusion'], 'reason': judgment['reason'], 'support_relations': []}
        for i, support in enumerate(supports):
            relation = relations[str(i)]
            if not isinstance(relation, dict) or set(relation) != {'support_sha256', 'relation', 'reason'}:
                raise ValidationError('invalid_adversarial_support_relation')
            if relation['support_sha256'] != references[cid][str(i)]:
                raise ValidationError('adversarial_support_digest_changed')
            item['support_relations'].append({'support_index': i, 'relation': relation['relation'],
                'reason': relation['reason'], 'passage_id': support['passage_id'], 'quote': support['quote']})
        expanded['claims'].append(item)
    for axis in AXES:
        value = raw['axes'][axis]
        if not isinstance(value, dict) or set(value) != {'status', 'reason', 'searched_passage_ids', 'findings'}:
            raise ValidationError('invalid_adversarial_axis')
        if value['searched_passage_ids'] != batch['searched_passage_ids']:
            raise ValidationError('adversarial_received_material_scope_not_covered')
        if plan.get('schema_profile') == SCHEMA_PROFILE_V4:
            searched = value['searched_passage_ids']
            if type(searched) is not list or any(type(pid) is not int for pid in searched):
                raise ValidationError('adversarial_received_material_scope_not_covered')
        expanded['axes'].append({'axis': axis, **deepcopy(value)})
    return expanded


def validate_adversarial_response(plan, batch, raw):
    """Legacy unchanged; v3 keeps its exact wire shape and binds selected supports."""
    _schema_profile(plan, batch)
    if 'response_contract' not in plan:
        if 'response_contract' in batch.get('packet', {}):
            raise ValidationError('adversarial_response_contract_mismatch')
        return _validate_legacy_adversarial_response(plan, batch, raw)
    if plan['response_contract'] != ADVERSARIAL_RESPONSE_V3:
        raise ValidationError('adversarial_response_contract_mismatch')
    expanded = _expand_v3_response(plan, batch, raw)
    # The received v3 wire remains bounded above. Quotes resolved internally from
    # the already bounded plan are not a second provider response or new proof.
    _validate_legacy_adversarial_response(plan, batch, expanded, _check_wire_bound=False)
    return raw


def expand_adversarial_response(plan, batch, raw):
    """Validated internal view only; persistence/readback retain raw v3 bytes."""
    validate_adversarial_response(plan, batch, raw)
    return _expand_v3_response(plan, batch, raw) if plan.get('response_contract') == ADVERSARIAL_RESPONSE_V3 else deepcopy(raw)


def aggregate_adversarial(plan, responses):
    if not isinstance(responses, dict) or not set(responses) <= {b['batch_id'] for b in plan['batches']}:
        raise ValidationError('adversarial_foreign_batch')
    if len(responses) != len(plan['batches']):
        return {'contract': CONTRACT, 'fingerprint': plan['fingerprint'], 'summary': {
            'status': 'incomplete', 'counterevidence_complete': False, 'no_result_is_not_absence_proof': True}}
    validated = [deepcopy(validate_adversarial_response(plan, b, responses[b['batch_id']])) for b in plan['batches']]
    effective = [_expand_v3_response(plan, b, raw) if plan.get('response_contract') == ADVERSARIAL_RESPONSE_V3 else raw
                 for b, raw in zip(plan['batches'], validated)]
    passages = _passages(plan['snapshot'])
    originals = {c['id']: c for c in plan['first_result']['claims']}
    findings = []
    for response in effective:
        for judgment in response['claims']:
            claim = originals[judgment['claim_id']]
            for relation in judgment['support_relations']:
                if relation['relation'] != 'supports':
                    entry = passages[str(relation['passage_id'])]
                    quote = relation['quote']; offset = entry['passage']['exact_text'].index(quote)
                    findings.append(_finding('quote_out_of_context', 'adversarial_selected_support_' + relation['relation'],
                        relation['reason'], claim=claim, proof=_proof(entry, quote,
                            before=entry['passage']['exact_text'][max(0, offset-100):offset],
                            after=entry['passage']['exact_text'][offset+len(quote):offset+len(quote)+100])))
            if judgment['conclusion'] != 'no_objection' and not any(r['relation'] != 'supports' for r in judgment['support_relations']):
                findings.append(_finding('contrary_interpretation', 'adversarial_claim_' + judgment['conclusion'],
                    judgment['reason'], claim=claim))
        for axis in response['axes']:
            for finding in axis['findings']:
                entry = passages[str(finding['passage_id'])]
                quote = finding['quote']; offset = entry['passage']['exact_text'].index(quote)
                findings.append(_finding(axis['axis'], 'adversarial_' + finding['relation'], finding['reason'],
                    claim=originals[finding['claim_id']], proof=_proof(entry, quote,
                        before=entry['passage']['exact_text'][max(0, offset-100):offset],
                        after=entry['passage']['exact_text'][offset+len(quote):offset+len(quote)+100])))
    unavailable = any(a['status'] == 'unavailable' for r in effective for a in r['axes'])
    objections = any(j['conclusion'] != 'no_objection' for r in effective for j in r['claims']) or any(a['status'] == 'found' for r in effective for a in r['axes'])
    report = {'contract': DEPENDENCY_CONTRACT if plan['snapshot'].get('conclusion') else CONTRACT, 'fingerprint': plan['fingerprint'], 'plan_digest': plan['plan_digest'],
              'first_result_digest': plan['first_result_digest'], 'origin': 'unattributed_adversarial_results',
              'responses': validated, 'runtime_identity_verified': False,
              'findings': findings,
              'axes': [{'axis': axis, 'batches': [{'batch_id': r['batch_id'], **deepcopy(a)}
                        for r in effective for a in r['axes'] if a['axis'] == axis],
                        'absence_proven': False} for axis in AXES],
              'summary': {'status': 'incomplete' if unavailable else ('objections_received' if objections else 'no_findings_in_received_material'),
                          'structural_response_complete': not unavailable, 'counterevidence_complete': False,
                          'finding_count': len(findings), 'no_result_is_not_absence_proof': True, 'publishable': False},
              'limitations': ['Résultats complets au contrat, identité/origine natives encore non reçues.',
                              'Aucun jugement défavorable du premier passage n’est changé par cette contre-lecture.']}
    if plan['snapshot'].get('conclusion'):
        graph = plan['classification']['dependency_graph']
        report['dependency_graph_digest'] = graph['graph_digest']
        for axis in report['axes']:
            axis['conclusion_applicability'] = deepcopy(graph['counterevidence_requirements'][axis['axis']])
    report['counterevidence_digest'] = sha256_digest(report)
    return report


def receive_native_adversarial(plan, responses, *, generation_revision, first_receipt,
                               counter_attempt, native_validator):
    """Server-only binding to fresh ORM actors and an independently checked run.

    native_validator is trusted server code that rechecks persisted native
    phase/return/packet hashes, not a boolean or function named in source JSON.
    This does not execute the model and never creates a human review.
    """
    from atelier.models import EditorialRevision, VerificationReceipt, VerificationAttempt, UserProfile
    from . import verification as verification
    if (not isinstance(generation_revision, EditorialRevision) or not generation_revision.pk
            or not isinstance(first_receipt, VerificationReceipt) or not first_receipt.pk
            or not isinstance(counter_attempt, VerificationAttempt) or not counter_attempt.pk
            or not callable(native_validator)):
        raise ValidationError('adversarial_runtime_receipts_required')
    revision = EditorialRevision.objects.select_related('dossier').get(pk=generation_revision.pk)
    first = VerificationReceipt.objects.select_related('attempt__actor', 'job').defer('attempt__actor__password').get(pk=first_receipt.pk)
    counter = VerificationAttempt.objects.select_related('actor', 'job').defer('actor__password').get(pk=counter_attempt.pk)
    if (revision.pk != plan['revision_id'] or revision.created_by_id is None
            or first.job.revision_id != revision.pk or counter.job.revision_id != revision.pk
            or first.fingerprint != plan['fingerprint'] or counter.job.fingerprint != plan['fingerprint']
            or first.attempt.stage != 'completed' or not first.attempt.finished_at
            or first.result_digest != sha256_digest(first.attempt.result)
            or counter.stage != 'completed' or not counter.finished_at or counter.result_digest != sha256_digest(counter.result)
            or counter.result != {'contract': 'dv.adversarial-executor/1', 'fingerprint': plan['fingerprint'],
                                  'plan_digest': plan['plan_digest'], 'responses': responses}
            or len({revision.created_by_id, first.attempt.actor_id, counter.actor_id}) != 3
            or not counter.execution_id or first.attempt.execution_id == counter.execution_id):
        raise ValidationError('adversarial_runtime_identity_or_revision_separation_failed')
    verification.require_verifier(first.attempt.actor)
    verification.require_verifier(counter.actor)
    if counter.model != plan['model'] or counter.effort != plan['effort']:
        raise ValidationError('adversarial_runtime_model_changed')
    synthetic = plan['snapshot'].get('scope') == 'synthetic'
    if not synthetic and (counter.provider != 'codex_session' or first.attempt.provider != 'codex_session'):
        raise ValidationError('adversarial_native_provider_receipt_required')
    if synthetic and not UserProfile.objects.filter(user_id=counter.actor_id, is_test_identity=True).exists():
        raise ValidationError('adversarial_synth_test_identity_required')
    if native_validator(counter, plan, responses) is not True:
        raise ValidationError('adversarial_native_response_not_received')
    report = aggregate_adversarial(plan, responses)
    if not report['summary'].get('structural_response_complete'):
        raise ValidationError('adversarial_response_incomplete')
    report.pop('counterevidence_digest')
    report.update(origin='SYNTH_qualified_receipts' if synthetic else 'provider_response_file', runtime_identity_verified=True,
        identity_check={'generation_actor_id': revision.created_by_id, 'first_judgment_actor_id': first.attempt.actor_id,
            'first_judgment_execution_id': first.attempt.execution_id, 'counter_actor_id': counter.actor_id,
            'counter_execution_id': counter.execution_id, 'declared_separation': True,
            'runtime_identity_verified': True},
        runtime_binding={'generation_actor_id': revision.created_by_id, 'first_receipt_id': first.pk,
            'first_judgment_actor_id': first.attempt.actor_id, 'counter_attempt_id': counter.pk,
            'first_judgment_execution_id': first.attempt.execution_id,
            'counter_actor_id': counter.actor_id, 'counter_execution_id': counter.execution_id,
            'counter_result_digest': counter.result_digest})
    report['summary']['counterevidence_complete'] = True
    report['counterevidence_digest'] = sha256_digest(report)
    return report
