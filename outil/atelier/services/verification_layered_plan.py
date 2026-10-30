"""Partition B only while retaining exact A/C/D coverage and full fingerprint.

Transport selectors and strict semantic translation remain the received 1.0
protocol. This plan/aggregation flavor is explicit and never passed off as an
unmodified historical plan or as a favorable legacy VerificationReceipt.
"""
from copy import deepcopy
import math
from types import SimpleNamespace

from django.core.exceptions import ValidationError

from .canonical import sha256_digest
from . import verification as v
from . import verification_protocol as p
from .proof_classification import validate_classification
from .verification_preflight import validate_preflight

CONTRACT = 'dv.layered-plan/1'
AGGREGATOR = 'dv.layered-aggregation/1'
RESULT = 'dv.layered-result/1'
CONTRACT_V2 = 'dv.layered-plan/2'
AGGREGATOR_V2 = 'dv.layered-aggregation/2'
RESULT_V2 = 'dv.layered-result/2'


def required_semantic(claim):
    return claim['layer'] == 'semantic' and claim.get('necessary_for_conclusion', True)


def _result_contract(plan):
    return RESULT_V2 if plan['snapshot'].get('conclusion') else RESULT


def build_plan(snapshot, *, classification, preflight, job_id, job_key, revision_id,
               owner_id, model, effort, limits=None, source_reuse_plan_digest=None,
               documentary_annex=None, source_root=None, received_sources=None):
    validate_classification(snapshot, classification, source_root=source_root, received_sources=received_sources)
    validate_preflight(snapshot, classification, preflight, source_root=source_root, received_sources=received_sources)
    limits = (limits or p.Limits()).validate()
    # Calling the received builder without layered arguments remains a local
    # bounds computation; no artifact, allocation, database or provider write.
    legacy_builder = getattr(p, '_build_plan_legacy', p.build_plan)
    plan = legacy_builder(snapshot, job_id=job_id, job_key=job_key, revision_id=revision_id,
        owner_id=owner_id, model=model, effort=effort, limits=limits,
        source_reuse_plan_digest=source_reuse_plan_digest, documentary_annex=documentary_annex)
    plan.pop('plan_digest')
    semantic = {c['claim_id'] for c in classification['claims'] if required_semantic(c)}
    identifiers = [a['assertion_id'] for a in plan['assertions'] if a['claim_id'] in semantic]
    plan['batches'] = [b for b in plan['batches'] if b['stage'] == 'evidence'] if semantic else []
    position, number = 0, 0
    while position < len(identifiers):
        count = min(limits.max_claims, len(identifiers) - position)
        while True:
            batch = {'batch_id': f'claims-{number:04d}', 'stage': 'claims',
                     'assertion_ids': identifiers[position:position + count]}
            try:
                batch['measurements'] = p._measure(plan, batch, limits)
                break
            except ValidationError:
                if count == 1:
                    raise
                count = max(1, count // 2)
        plan['batches'].append(batch)
        position += count
        number += 1
    source_phases = sum(b['stage'] == 'evidence' for b in plan['batches'])
    semantic_phases = sum(b['stage'] == 'claims' for b in plan['batches'])
    # Every adverse read must be independently received. It is forecast in the
    # allocation and explicit as a subsequent plan, never silently executed by
    # the first-pass runner. Reuse requires its native producer receipt at run.
    new_source = 0 if source_reuse_plan_digest else source_phases
    counter_max_claims = preflight['cost']['counter_max_claims_per_phase']
    counter_phases = math.ceil(len(semantic) / counter_max_claims)
    plan['proof_architecture'] = {'contract': CONTRACT_V2 if snapshot.get('conclusion') else CONTRACT,
        'classification': deepcopy(classification), 'preflight': deepcopy(preflight),
        'classification_digest': classification['classification_digest'],
        'preflight_digest': preflight['preflight_digest'],
        'nonsemantic_coverage': [deepcopy(c) for c in classification['claims'] if not required_semantic(c)],
        'unknown_annotations': deepcopy(classification['unknowns']),
        'semantic_claim_ids': [c['claim_id'] for c in classification['claims'] if required_semantic(c)],
        'independent_counterevidence_required': True, 'counter_max_claims_per_phase': counter_max_claims}
    plan['aggregator'] = AGGREGATOR_V2 if snapshot.get('conclusion') else AGGREGATOR
    plan['allocation_proposal'] = {'authorization': 'NOT_AUTHORIZED',
        'source_phases': source_phases, 'reused_source_phases': source_phases - new_source,
        'claim_phases': semantic_phases, 'counterevidence_phases': counter_phases,
        'counter_max_claims_per_phase': counter_max_claims,
        'first_pass_minimum_new_phases': new_source + semantic_phases,
        'minimum_new_phases': new_source + semantic_phases + counter_phases,
        'resume_margin_phases': limits.resume_margin_phases,
        'proposed_ceiling': new_source + semantic_phases + counter_phases + limits.resume_margin_phases,
        'actual_phases_reserved': 0, 'actual_phases_launched': 0}
    plan['plan_digest'] = sha256_digest(plan)
    return plan


def validate_plan(plan, *, source_root=None, received_sources=None):
    architecture = plan.get('proof_architecture', {}) if isinstance(plan, dict) else {}
    version2 = isinstance(plan, dict) and bool(plan.get('snapshot', {}).get('conclusion'))
    if architecture.get('contract') != (CONTRACT_V2 if version2 else CONTRACT) or plan.get('aggregator') != (AGGREGATOR_V2 if version2 else AGGREGATOR):
        raise ValidationError('layered_plan_contract_required')
    try:
        rebuilt = build_plan(plan['snapshot'], classification=architecture['classification'],
            preflight=architecture['preflight'], job_id=plan['job_id'], job_key=plan['job_key'],
            revision_id=plan['revision_id'], owner_id=plan['owner_id'], model=plan['model'], effort=plan['effort'],
            limits=p.Limits(**plan['limits']), source_reuse_plan_digest=plan['source_reuse_plan_digest'],
            documentary_annex=plan.get('documentary_annex'), source_root=source_root, received_sources=received_sources)
    except (KeyError, TypeError):
        raise ValidationError('layered_plan_structure_invalid') from None
    if rebuilt != plan:
        raise ValidationError('layered_plan_routing_or_digest_changed')
    return plan


def packet_for(plan, batch):
    # The complete source inventory, context and counter-elements are retained
    # by the qualified transport, despite routing only B to semantic claims.
    legacy_packet = getattr(p, '_packet_for_legacy', p.packet_for)
    packet = legacy_packet(plan, batch)
    if batch['stage'] == 'claims':
        routed = set(plan['proof_architecture']['semantic_claim_ids'])
        if any(a['claim_id'] not in routed for a in packet['assertions']):
            raise ValidationError('nonsemantic_assertion_in_model_packet')
    return packet


def aggregate(plan, states):
    architecture = plan['proof_architecture']
    expected = {b['batch_id']: b for b in plan['batches'] if b['stage'] == 'claims'}
    if not isinstance(states, dict) or not set(states) <= set(expected):
        raise ValidationError('foreign_layered_aggregation_batch')
    semantic = {}
    batches = []
    for bid, batch in expected.items():
        state = states.get(bid, {'state': 'prepared'})
        batches.append({'batch_id': bid, 'state': state.get('state')})
        if state.get('state') != 'accepted':
            return {'execution': 'incomplete', 'documentary': 'not_determined',
                    'diffusion': 'ineligible', 'batches': batches, 'result': None}
        result = state.get('result')
        wanted = {a['claim_id'] for a in plan['assertions'] if a['assertion_id'] in batch['assertion_ids']}
        if (not isinstance(result, dict) or result.get('contract') != plan['policy']
                or result.get('fingerprint') != plan['fingerprint']
                or not isinstance(result.get('claims'), list)
                or len(result['claims']) != len(wanted)
                or {c.get('id') for c in result['claims']} != wanted):
            raise ValidationError('layered_semantic_coverage_changed')
        local_snapshot = deepcopy(plan['snapshot'])
        local_snapshot['claims'] = [c for c in local_snapshot['claims'] if c['id'] in wanted]
        v._check_result(SimpleNamespace(input_snapshot=local_snapshot, fingerprint=plan['fingerprint']), result)
        for claim in result['claims']:
            if claim['id'] in semantic:
                raise ValidationError('layered_semantic_duplicate')
            semantic[claim['id']] = deepcopy(claim)
    originals = {c['id']: c for c in plan['snapshot']['claims']}
    coverage, issues = [], []
    for route in architecture['classification']['claims']:
        original = originals[route['claim_id']]
        layer = route['layer']
        record = {'id': original['id'], 'location': original['location'],
                  'value': deepcopy(original['value']), 'layer': layer,
                  'proof_reference': route['certificate_id'] or route['selection_rule_id'],
                  'semantic_judgment': None, 'necessary_for_conclusion': route['necessary_for_conclusion']}
        if plan['snapshot'].get('conclusion') and not route['necessary_for_conclusion']:
            record['status'] = 'not_applicable'
            record['proof_reference'] = route['dependency_rule_id']
        elif layer == 'semantic':
            judgment = semantic.get(original['id'])
            if judgment is None:
                raise ValidationError('layered_semantic_claim_missing')
            record.update(status=judgment['verdict'], semantic_judgment=judgment)
            if (judgment['verdict'] != 'ETAYE_DANS_PERIMETRE' or judgment['against']
                    or not v.action_satisfied(judgment)):
                issues.append(original['id'])
        elif layer == 'deterministic':
            record['status'] = route.get('provenance_status', 'missing')
            if record['status'] != 'proven':
                issues.append(original['id'])
        elif layer == 'editorial':
            record['status'] = 'editorial_selection'
        else:
            record['status'] = 'explicit_unknown'
            if record['necessary_for_conclusion']:
                issues.append(original['id'])
        coverage.append(record)
    unknown_annotations = deepcopy(architecture['classification']['unknowns'])
    covered_ids = {c['id'] for c in coverage}
    issues.extend(u['claim_id'] for u in unknown_annotations
                  if u['necessary_for_conclusion'] and u['claim_id'] not in covered_ids)
    result = {'contract': _result_contract(plan), 'fingerprint': plan['fingerprint'],
              'classification_digest': architecture['classification_digest'],
              'preflight_digest': architecture['preflight_digest'], 'claims': coverage,
              'unknown_annotations': unknown_annotations,
              'unknown_fields_count': len({c['location'] for c in coverage if c['layer'] == 'unknown'}
                                         | {u['location'] for u in unknown_annotations}),
              'first_pass_complete': True, 'semantic_complete': True,
              'counterevidence_complete': False, 'human_reviewed_exact_revision': False,
              'documentary': 'not_supported' if issues else 'insufficient_evidence',
              'issues': issues,
              'limitations': ['Exécution du premier jugement seulement ; contre-preuve indépendante et revue personnelle distinctes.']}
    result['result_digest'] = sha256_digest(result)
    return {'execution': 'complete', 'documentary': result['documentary'],
            'diffusion': 'not_authorized', 'batches': batches, 'result': result}


def semantic_result(plan, result):
    """Exact B-only legacy-shaped input for citation/counterevidence controls."""
    if result.get('contract') != _result_contract(plan) or result.get('fingerprint') != plan['fingerprint']:
        raise ValidationError('layered_result_binding_changed')
    judgments = [deepcopy(c['semantic_judgment']) for c in result['claims'] if c['semantic_judgment'] is not None]
    return {'contract': plan['policy'], 'fingerprint': plan['fingerprint'], 'claims': judgments,
            'coverage_statement': 'Couche sémantique B seule, empreinte complète inchangée.',
            'limitations': deepcopy(result['limitations'])}


def validate_result(plan, result):
    if not isinstance(result, dict) or result.get('contract') != _result_contract(plan):
        raise ValidationError('layered_result_contract_required')
    states = {}
    by_id = {c['id']: c for c in result.get('claims', [])}
    for batch in plan['batches']:
        if batch['stage'] != 'claims':
            continue
        wanted = [a['claim_id'] for a in plan['assertions'] if a['assertion_id'] in batch['assertion_ids']]
        try:
            judgments = [deepcopy(by_id[cid]['semantic_judgment']) for cid in wanted]
        except KeyError:
            raise ValidationError('layered_result_semantic_missing') from None
        states[batch['batch_id']] = {'state': 'accepted', 'result': {
            'contract': plan['policy'], 'fingerprint': plan['fingerprint'], 'claims': judgments,
            'coverage_statement': 'Contrôle local de résultat par couche.', 'limitations': []}}
    rebuilt = aggregate(plan, states)['result']
    if rebuilt != result:
        raise ValidationError('layered_result_coverage_or_status_changed')
    return result
