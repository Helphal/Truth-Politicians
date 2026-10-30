"""File documentaire pure : une objection instruite n'est jamais un reçu.

Les groupes du fournisseur restent intacts, même incomplets ou défavorables.
La qualification séparée est liée aux octets du brut et à l'instantané. Aucune
requête DB, inférence, réparation de citation ou décision de diffusion ici.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import re

from .canonical import canonical_bytes, sha256_digest


CONTRACT = 'dv.objections/1'
REVIEW_CONTRACT = 'dv.verification-review-artifact/1'
QUALIFICATIONS = {
    'error_confirmed': 'erreur confirmée',
    'missing_evidence': 'preuve manquante',
    'schema_context_ambiguity': 'ambiguïté schéma/contexte',
    'refuted_with_evidence': 'objection réfutée avec preuve',
    'unresolved_limit': 'limite non levée',
}
EVIDENCE_FAMILIES = {
    'institutional_fact', 'declaration_attribution', 'derived_relation',
    'editorial_perimeter', 'application_state', 'rights_usage',
}
FAVORABLE = 'ETAYE_DANS_PERIMETRE'
HEX64 = re.compile(r'^[0-9a-f]{64}$')


class ObjectionContractError(ValueError):
    """Refus fermé, avec code stable ne contenant aucune pièce source."""


def _fail(code):
    raise ObjectionContractError(code)


def resolve_pointer(value, pointer):
    if pointer == '':
        return value
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        _fail('invalid_pointer')
    try:
        for part in pointer[1:].split('/'):
            if re.search(r'~(?![01])', part):
                _fail('invalid_pointer_escape')
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', part):
                    _fail('invalid_array_index')
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                _fail('pointer_not_container')
    except (KeyError, IndexError):
        _fail('pointer_not_found')
    return value


def _equal(left, right):
    # JSON bool et entier sont distincts malgré True == 1 en Python.
    return canonical_bytes(left) == canonical_bytes(right)


def _family(location):
    """Routage de revue seulement ; ne change pas structural_metadata/policy."""
    if '/diffusion_selection/public_note' in location:
        return 'rights_usage'
    if '/diffusion_selection/passages/' in location and location.endswith('/attribution'):
        return 'declaration_attribution'
    if '/identity_status' in location:
        return 'application_state'
    if '/coverage/' in location or location.endswith('/ambiguity') or location.endswith('/main_caveat'):
        return 'editorial_perimeter'
    if any(part in location for part in ('/result', '/reasoning', '/short_answer', '/text_reference_role', '/relation', '/version_label', '/collective_vote_distribution/')):
        return 'derived_relation'
    if '/actors/' in location or '/attribution/' in location:
        return 'declaration_attribution'
    return 'institutional_fact'


def _passages(snapshot):
    found = {}
    for source in snapshot.get('sources', []):
        for passage in source.get('passages', []):
            key = passage.get('id')
            if type(key) is not int or key in found:
                _fail('source_passage_identity_ambiguous')
            found[key] = (source, passage)
    return found


def _source_references(group, passages):
    result = []
    for role in ('support', 'against'):
        for ref in group.get(role, []):
            if not isinstance(ref, dict):
                _fail('invalid_model_reference')
            item = {'role': role, 'raw_reference': deepcopy(ref),
                    'passage_id': ref.get('passage_id'), 'quote_literal': False}
            if type(ref.get('passage_id')) is int and ref['passage_id'] in passages:
                source, passage = passages[ref['passage_id']]
                text = passage.get('exact_text', '')
                item.update(source_version=deepcopy(source.get('reference')),
                    locator=passage.get('locator'), public_url=passage.get('public_url'),
                    exact_text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    quote_literal=isinstance(ref.get('quote'), str) and bool(ref['quote']) and ref['quote'] in text)
            result.append(item)
    return result


def _proof(proof, snapshot, passages, source_originals):
    if not isinstance(proof, dict):
        _fail('invalid_assessment_proof')
    result = deepcopy(proof)
    kind = proof.get('kind')
    if kind == 'snapshot_pointer':
        if 'expected' not in proof or not _equal(resolve_pointer(snapshot['content'], proof.get('pointer')), proof['expected']):
            _fail('assessment_snapshot_proof_mismatch')
        result['verification'] = 'exact_snapshot_value'
    elif kind == 'source_passage':
        key, quote = proof.get('passage_id'), proof.get('quote')
        if type(key) is not int or key not in passages or not isinstance(quote, str) or not quote:
            _fail('assessment_passage_missing')
        source, passage = passages[key]
        if quote not in passage['exact_text']:
            _fail('assessment_quote_not_literal')
        if proof.get('source_version') and not _equal(source['reference'], proof['source_version']):
            _fail('assessment_source_binding_mismatch')
        result.update(source_version=deepcopy(source['reference']), locator=passage['locator'],
                      verification='literal_in_pinned_passage')
    elif kind == 'pinned_original_segment':
        digest = proof.get('original_sha256')
        reference = proof.get('source_version')
        matches = [s for s in snapshot.get('sources', []) if _equal(s.get('reference'), reference)]
        if len(matches) != 1 or not isinstance(reference, dict) or reference.get('sha256') != digest:
            _fail('assessment_original_not_pinned')
        data = source_originals.get(digest)
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
            _fail('assessment_original_hash_mismatch')
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            _fail('assessment_original_encoding')
        start, end, segment = proof.get('start'), proof.get('end'), proof.get('exact_segment')
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(text) or text[start:end] != segment:
            _fail('assessment_original_segment_mismatch')
        result.update(verification='exact_segment_in_pinned_original', offset_unit='unicode_codepoints',
                      segment_sha256=hashlib.sha256(segment.encode('utf-8')).hexdigest())
    elif kind == 'bound_artifact':
        # Le fichier est relu/haché par le lecteur de pièces ; aucun chemin de
        # pièce ne peut provoquer ici une ouverture arbitraire ou une exécution.
        if not isinstance(proof.get('path'), str) or not HEX64.fullmatch(str(proof.get('sha256', ''))) or not isinstance(proof.get('observation'), str) or not proof['observation']:
            _fail('invalid_bound_artifact_proof')
        result['verification'] = 'external_readback_required'
    else:
        _fail('assessment_proof_kind_unknown')
    return result


def _assessment(value, group_digest, binding, snapshot, passages, source_originals):
    if value is None:
        return {'code': 'pending', 'label': 'à instruire', 'rationale': '', 'proofs': [],
                'required_action': 'Instruire sans adopter le verdict du modèle.', 'case_ids': []}
    if not isinstance(value, dict) or value.get('code') not in QUALIFICATIONS:
        _fail('assessment_qualification_unknown')
    if value.get('raw_sha256') != binding['raw_sha256'] or value.get('group_sha256') != group_digest or value.get('fingerprint') != binding['fingerprint']:
        _fail('assessment_binding_changed')
    if not isinstance(value.get('rationale'), str) or not value['rationale'].strip() or not isinstance(value.get('required_action'), str) or not value['required_action'].strip():
        _fail('assessment_reason_or_action_missing')
    if not isinstance(value.get('proofs'), list) or not value['proofs']:
        _fail('assessment_proof_required')
    families = value.get('evidence_families', [])
    if not isinstance(families, list) or not families or any(f not in EVIDENCE_FAMILIES for f in families):
        _fail('assessment_evidence_family_unknown')
    result = deepcopy(value)
    result['label'] = QUALIFICATIONS[value['code']]
    result['proofs'] = [_proof(p, snapshot, passages, source_originals) for p in value['proofs']]
    result['authorship'] = 'documentary_assessment_separate_from_provider'
    return result


def build_objection_queue(snapshot, raw_result, binding, *, model_claims=None,
                          assessments=None, source_originals=None):
    """Résout les indices du paquet effectif vers l'instantané complet.

    `model_claims` est la liste effectivement envoyée ; elle n'est jamais une
    nouvelle sélection de métadonnées. Les trous et doublons restent exposés.
    Les qualifications facultatives ne changent aucun groupe du fournisseur.
    """
    if not isinstance(snapshot, dict) or not isinstance(raw_result, dict) or not isinstance(binding, dict):
        _fail('objection_input_type')
    for name in ('job_id', 'revision_pk', 'attempt_id'):
        if type(binding.get(name)) is not int or binding[name] < 1:
            _fail('objection_identity_required')
    if not HEX64.fullmatch(str(binding.get('raw_sha256', ''))):
        _fail('raw_sha256_required')
    if sha256_digest(snapshot) != binding.get('fingerprint') or raw_result.get('fingerprint') != binding['fingerprint']:
        _fail('snapshot_or_raw_fingerprint_mismatch')
    if binding.get('revision') != snapshot.get('revision') or binding.get('object') != snapshot.get('object'):
        _fail('revision_or_object_mismatch')
    if raw_result.get('contract') != 'dv.compact-claims/1' or not isinstance(raw_result.get('groups'), list):
        _fail('historical_group_contract_required')
    full_claims = snapshot.get('claims')
    if not isinstance(full_claims, list):
        _fail('snapshot_claims_missing')
    by_id = {c.get('id'): c for c in full_claims if isinstance(c, dict)}
    if len(by_id) != len(full_claims) or any(not isinstance(k, str) or not k for k in by_id):
        _fail('snapshot_claim_identity_ambiguous')
    selected = full_claims if model_claims is None else model_claims
    if not isinstance(selected, list) or not selected:
        _fail('model_claims_missing')
    ids = []
    for claim in selected:
        if not isinstance(claim, dict) or claim.get('id') not in by_id:
            _fail('model_claim_not_in_snapshot')
        expected = by_id[claim['id']]
        if not _equal({k: claim.get(k) for k in ('id', 'location', 'value')}, expected):
            _fail('model_claim_mapping_changed')
        if not _equal(resolve_pointer(snapshot['content'], expected['location']), expected['value']):
            _fail('snapshot_claim_value_changed')
        ids.append(claim['id'])
    if len(set(ids)) != len(ids):
        _fail('model_claim_identity_ambiguous')
    assessments, originals = assessments or {}, source_originals or {}
    if not isinstance(assessments, dict) or not isinstance(originals, dict):
        _fail('assessment_input_type')
    passages = _passages(snapshot)
    counter = Counter()
    objections = []
    consumed_assessments = set()
    for group_index, group in enumerate(raw_result['groups']):
        if not isinstance(group, dict) or not isinstance(group.get('verdict'), str) or not isinstance(group.get('claim_indices'), list) or not group['claim_indices']:
            _fail('invalid_model_group')
        for index in group['claim_indices']:
            if type(index) is not int or not 0 <= index < len(selected):
                _fail('model_claim_index_out_of_range')
            counter[index] += 1
        if group['verdict'] == FAVORABLE:
            continue
        group_digest = sha256_digest(group)
        key = f"{binding['job_id']}:{group_index}"
        identifier = sha256_digest([binding['job_id'], binding['attempt_id'], binding['raw_sha256'], group_index])[:24]
        affected = []
        for index in group['claim_indices']:
            claim = selected[index]
            parent = claim['location'].rsplit('/', 1)[0]
            affected.append({k: deepcopy(claim[k]) for k in ('id', 'location', 'value')})
            affected[-1].update(claim_index=index, context_location=parent,
                context=deepcopy(resolve_pointer(snapshot['content'], parent)),
                evidence_family=_family(claim['location']), family_basis='review_routing_no_validation_exemption')
        assessment = assessments.get(key)
        if assessment is not None:
            consumed_assessments.add(key)
        objections.append({'id': identifier, 'key': key, 'raw_group_index': group_index,
            'raw_group_sha256': group_digest, 'model_group': deepcopy(group),
            'claims': affected, 'source_references': _source_references(group, passages),
            'qualification': _assessment(assessment, group_digest, binding, snapshot, passages, originals)})
    if consumed_assessments != set(assessments):
        _fail('assessment_group_not_present')
    missing = sorted(set(range(len(selected))) - set(counter))
    payload = snapshot.get('content', {}).get('public_payload', {})
    context = {k: deepcopy(snapshot['content'].get(k)) for k in
               ('title', 'short_answer', 'main_caveat', 'reasoning', 'result', 'method_version')}
    context.update({k: deepcopy(payload.get(k)) for k in ('actors', 'scope', 'counterevidence')})
    return {'contract': CONTRACT, 'binding': deepcopy(binding),
        'raw_result_canonical_sha256': sha256_digest(raw_result),
        'context': context, 'objections': objections,
        'coverage': {'expected_count': len(selected), 'present_count': len(counter),
            'missing_count': len(missing), 'missing_indices': missing,
            'duplicate_count': sum(v - 1 for v in counter.values()),
            'duplicate_indices': sorted(i for i, count in counter.items() if count > 1),
            'non_favorable_groups': len(objections)},
        'axes': {'execution': 'historical_refused_response',
                 'documentary': 'objections_instructed' if all(o['qualification']['code'] != 'pending' for o in objections) else 'objections_pending',
                 'diffusion': 'not_eligible'},
        'is_verification_receipt': False, 'human_review': False, 'publication': False,
        'model_judgments_changed': False}


def build_review_artifact(snapshot, raw_result, binding, *, model_claims=None,
                          assessments=None, source_originals=None):
    queue = build_objection_queue(snapshot, raw_result, binding, model_claims=model_claims,
                                 assessments=assessments, source_originals=source_originals)
    return {'contract': REVIEW_CONTRACT, **deepcopy(binding),
            **queue['coverage'], 'raw_file': 'response.json',
            'raw_result_canonical_sha256': queue['raw_result_canonical_sha256'],
            'context': queue['context'], 'objections': queue['objections'],
            'axes': queue['axes'], 'is_verification_receipt': False,
            'human_review': False, 'publication': False, 'model_judgments_changed': False}


def prepare_versioned_proposal(snapshot, binding, replacements, *, proposal_id, rationale, proofs):
    """Diff documentaire explicite, sans appliquer une révision ou une revue.

    Les anciennes valeurs sont vérifiées ; les nouveaux objets sont conservés
    séparément. Une future révision devra être reconstruite par les services
    normaux et passer toutes les validations et gates.
    """
    if sha256_digest(snapshot) != binding.get('fingerprint') or not isinstance(proposal_id, str) or not proposal_id:
        _fail('proposal_binding_changed')
    if not isinstance(rationale, str) or not rationale.strip() or not isinstance(proofs, list) or not proofs:
        _fail('proposal_reason_or_proof_missing')
    if not isinstance(replacements, list) or not replacements:
        _fail('proposal_diff_missing')
    used, changes = set(), []
    for change in replacements:
        if not isinstance(change, dict) or set(change) != {'location', 'before', 'after'}:
            _fail('proposal_diff_contract')
        location = change['location']
        if location in used or not _equal(resolve_pointer(snapshot['content'], location), change['before']):
            _fail('proposal_before_changed')
        if _equal(change['before'], change['after']):
            _fail('proposal_no_change')
        used.add(location)
        changes.append(deepcopy(change))
    return {'contract': 'dv.documentary-proposal/1', 'id': proposal_id,
        'source_binding': deepcopy(binding), 'status': 'proposed_not_applied',
        'target_revision': snapshot['revision'] + 1, 'rationale': rationale,
        'proofs': deepcopy(proofs), 'changes': changes,
        'source_revision_unchanged': True, 'requires_normal_reconstruction': True,
        'verification_receipt': False, 'human_review': False, 'publication': False}


def prepare_compatible_documentary_candidate(snapshot, binding, replacements, *,
                                             proposal_id, rationale, proofs):
    """Candidat détaché minimal : seulement des chaînes du contrat existant.

    Le serveur appelant reconstruit ensuite les assertions avec son extracteur
    normal et valide le schéma et les invariants sémantiques normaux. Cette
    fonction ne crée pas de révision, de job ni de contrôle. Elle ne peut changer
    aucun résultat, citation, vote, affiliation, source ou réserve centrale.
    """
    allowed = re.compile(
        r'^/public_payload/(?:'
        r'catalogue/legislative_dossiers/[0-9]+/(?:official_title|official_url|status)|'
        r'catalogue/text_versions/[0-9]+/official_title|'
        r'catalogue/scrutinies/[0-9]+/procedure|'
        r'method_explanation/(?:attribution|object|scope|chronology|procedure|counterevidence)|'
        r'coverage/statement)$')
    for change in replacements:
        if (not isinstance(change, dict) or not allowed.fullmatch(str(change.get('location', '')))
                or type(change.get('before')) is not str or type(change.get('after')) is not str
                or not change['after'].strip()):
            _fail('compatible_candidate_change_not_allowed')
    proposal = prepare_versioned_proposal(snapshot, binding, replacements,
        proposal_id=proposal_id, rationale=rationale, proofs=proofs)
    content = deepcopy(snapshot['content'])
    for change in replacements:
        path, leaf = change['location'].rsplit('/', 1)
        container = resolve_pointer(content, path)
        container[leaf.replace('~1','/').replace('~0','~')] = change['after']
    if set(content) != set(snapshot['content']):
        _fail('compatible_candidate_structure_changed')
    proposal.update(contract_evolution_required=False, current_contract_unchanged=True,
        protected_fields_unchanged=True, rebuild_assertions_with_normal_extractor=True,
        requires_normal_schema_and_semantic_validation=True)
    return {'contract': 'dv.compatible-documentary-candidate/1', 'proposal': proposal,
            'candidate_content': content, 'target_revision': proposal['target_revision'],
            'applied': False, 'verification_receipt': False, 'human_review': False,
            'publication': False}
