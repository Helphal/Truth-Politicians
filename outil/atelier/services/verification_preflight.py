"""Local fail-closed receipt before semantic allocation or provider use."""
from copy import deepcopy
from datetime import date
import math
import re

from django.core.exceptions import ValidationError
from django.utils import timezone

from .canonical import sha256_digest
from .proof_classification import validate_classification
from .verification_protocol import source_read_identity

CONTRACT = 'dv.verification-preflight/1'
CONTRACT_V2 = 'dv.verification-preflight/2'
RIGHTS = 'dv.rights-preflight/1'
REASONS = (
    'declaration_source_missing', 'exact_passage_not_found',
    'vote_exact_not_identified', 'vote_object_ambiguous',
    'actor_not_linked_at_relevant_date', 'required_vote_text_version_unestablished',
    'different_authors_as_personal_behavior', 'too_many_unknowns_for_conclusion',
    'rights_incompatible_with_render_mode',
)
RENDER_MODES = frozenset({'private_review', 'public_excerpt', 'public_long_reproduction', 'official_link'})


def _reference(value):
    return (value.get('source_id'), value.get('version_id'), value.get('sha256')) if isinstance(value, dict) else None


def _available_sources(snapshot):
    allowed_integrity = {'original_and_passages_checked'}
    if snapshot.get('scope') == 'synthetic':
        allowed_integrity.add('synthetic_no_original')
    return {_reference(s.get('reference')): s for s in snapshot.get('sources', [])
            if s.get('state') == 'available' and s.get('integrity') in allowed_integrity
            and _reference(s.get('reference')) is not None}


def _source(snapshot, reference):
    return _available_sources(snapshot).get(_reference(reference))


def _check(code, passed, reason, locations=(), *, missing=False):
    return {'code': code, 'status': 'pass' if passed else ('hold' if missing else 'reject'),
            'locations': list(locations), 'reason': reason}


def _rights(snapshot, report, mode, as_of):
    if report is None:
        return False, True, 'Qualification de cet usage et de cette version absente.'
    if not isinstance(report, dict) or report.get('contract') != RIGHTS:
        raise ValidationError('preflight_rights_contract_required')
    body = deepcopy(report)
    digest = body.pop('rights_digest', None)
    if (digest != sha256_digest(body) or body.get('fingerprint') != sha256_digest(snapshot)
            or body.get('render_mode') != mode or not isinstance(body.get('usages'), list)):
        raise ValidationError('preflight_rights_binding_changed')
    entries = {}
    for item in body['usages']:
        if not isinstance(item, dict) or set(item) != {'source_id', 'version_id', 'source_sha256', 'usage', 'status', 'qualification_reference', 'expires_on'}:
            raise ValidationError('invalid_preflight_rights_usage')
        key = (item['source_id'], item['version_id'], item['source_sha256'])
        if key in entries or item['usage'] != mode or item['status'] not in {'compatible', 'incompatible', 'unqualified'}:
            raise ValidationError('invalid_preflight_rights_usage')
        if not isinstance(item['qualification_reference'], str) or not item['qualification_reference'].strip():
            raise ValidationError('rights_qualification_reference_required')
        if item['expires_on'] is not None:
            try:
                expired = date.fromisoformat(item['expires_on']) < as_of
            except (TypeError, ValueError):
                raise ValidationError('invalid_rights_expiry') from None
            if expired:
                return False, True, 'Qualification datée périmée ; aucun droit actuel déduit.'
        entries[key] = item
    required = {_reference(s.get('reference')) for s in snapshot.get('sources', [])}
    if None in required or not required <= set(entries):
        return False, True, 'Toutes les versions nécessaires ne sont pas qualifiées pour ce rendu.'
    statuses = {entries[key]['status'] for key in required}
    if 'incompatible' in statuses:
        return False, False, 'Droit incompatible avec le mode de rendu prévu.'
    if 'unqualified' in statuses:
        return False, True, 'Droit non qualifié ; une ancienne mention ne vaut pas autorisation générale.'
    return True, False, 'Usages/version/mode liés à une qualification explicite, sans conclusion juridique générale.'


def _proven(classification, location):
    return any(c['location'] == location and c['layer'] == 'deterministic'
               and c.get('provenance_status') == 'proven'
               and c['fact_type'] != 'projection_binding' for c in classification['claims'])


def estimate_cost(snapshot, classification, *, max_claims=32, counter_max_claims=8, resume_margin_phases=2,
                  reuse_receipts=(), reuse_validator=None):
    if (type(max_claims) is not int or not 1 <= max_claims <= 32
            or type(counter_max_claims) is not int or not 1 <= counter_max_claims <= 8
            or type(resume_margin_phases) is not int or not 0 <= resume_margin_phases <= 64):
        raise ValidationError('invalid_preflight_cost_bounds')
    counts = {layer: sum(c['layer'] == layer for c in classification['claims'])
              for layer in ('deterministic', 'semantic', 'editorial', 'unknown')}
    full_counts = deepcopy(counts)
    layered_dependencies = classification.get('contract') == 'dv.proof-classification/2'
    if layered_dependencies:
        counts['semantic'] = sum(c['layer'] == 'semantic' and c['necessary_for_conclusion']
                                 for c in classification['claims'])
    sources = len(snapshot.get('sources', [])) if counts['semantic'] else 0
    reuses = []
    identity = source_read_identity(snapshot)
    for receipt in reuse_receipts:
        if not isinstance(receipt, dict):
            raise ValidationError('invalid_preflight_source_reuse')
        # Only a trusted runtime validator can admit reuse. JSON status/origin
        # strings alone affect no minimum budget.
        if (reuse_validator is not None and reuse_validator(receipt) is True
                and receipt.get('source_read_identity') == identity
                and receipt.get('origin') == 'provider_response_file'
                and receipt.get('state') == 'accepted'
                and type(receipt.get('source_phases')) is int
                and receipt['source_phases'] > 0):
            reuses.append({'receipt_digest': sha256_digest(receipt),
                           'source_phases': receipt['source_phases'],
                           'independent_read': False})
    reusable = min(sources, sum(x['source_phases'] for x in reuses))
    semantic = math.ceil(counts['semantic'] / max_claims)
    adversarial = math.ceil(counts['semantic'] / counter_max_claims)
    minimum = sources - reusable + semantic + adversarial
    report = {'deterministic_assertions': counts['deterministic'],
            'semantic_assertions': counts['semantic'],
            'editorial_assertions': counts['editorial'],
            'unknown_assertions': counts['unknown'] + sum(u['claim_id'] not in {c['claim_id'] for c in classification['claims']} for u in classification['unknowns']),
            'source_readings': sources, 'admissible_reuses': reuses,
            'reused_source_phases': reusable, 'source_model_phases': sources - reusable,
            'semantic_model_phases': semantic, 'counterevidence_model_phases': adversarial,
            'minimum_model_phases': minimum, 'proposed_ceiling': minimum + resume_margin_phases if minimum else 0,
            'max_claims_per_phase': max_claims, 'counter_max_claims_per_phase': counter_max_claims,
            'resume_margin_phases': resume_margin_phases,
            'estimate_basis': 'one_received_source_version_per_read_and_bounded_claim_batches; measured_plan_may_raise_ceiling',
            'authorization': 'NOT_AUTHORIZED', 'actual_phases_reserved': 0, 'actual_phases_launched': 0}
    if layered_dependencies:
        report['full_assertions_by_layer'] = full_counts
        report['required_assertions_by_layer'] = {
            layer: sum(c['layer'] == layer and c['necessary_for_conclusion'] for c in classification['claims'])
            for layer in counts}
        report['not_applicable_assertions'] = sum(not c['necessary_for_conclusion'] for c in classification['claims'])
        report['estimate_basis'] += '; semantic_batches_only_for_server_required_B; complete_snapshot_fingerprint_retained'
    return report


def _build_dependency_preflight(snapshot, classification, *, render_mode, rights_report,
                               day, max_claims, counter_max_claims, resume_margin_phases,
                               reuse_receipts, reuse_validator):
    graph = classification['dependency_graph']
    checks = []
    for dependency in graph['checks']:
        code, status = dependency['code'], dependency['status']
        if status == 'not_applicable':
            state, reason = 'not_applicable', dependency['why_required_for_this_conclusion']
        elif status == 'established':
            state, reason = 'pass', 'Dépendance documentaire reçue pour cette conclusion ; aucun jugement de sens déduit.'
        else:
            state = 'reject' if code == 'different_authors_as_personal_behavior' else 'hold'
            reason = dependency['why_required_for_this_conclusion'] + ' Pièce requise absente ou non établie.'
        checks.append({**deepcopy(dependency), 'status': state, 'reason': reason})
    rights_ok, rights_missing, rights_reason = _rights(snapshot, rights_report, render_mode, day)
    right_rule = graph['check_applicability']['rights_incompatible_with_render_mode']
    checks.append({'code': 'rights_incompatible_with_render_mode', **deepcopy(right_rule),
        'status': 'pass' if rights_ok else ('hold' if rights_missing else 'reject'),
        'locations': ['/public_payload/evidence'], 'blocking_claim_ids': [],
        'reason': rights_reason, 'absence_proven': False})
    status = 'rejected' if any(c['status'] == 'reject' for c in checks) else ('hold' if any(c['status'] == 'hold' for c in checks) else 'ready')
    report = {'contract': CONTRACT_V2, 'fingerprint': sha256_digest(snapshot),
        'classification_digest': classification['classification_digest'],
        'dependency_graph_digest': graph['graph_digest'], 'conclusion_digest': graph['conclusion_digest'],
        'archetype': graph['archetype'], 'render_mode': render_mode,
        'as_of': day.isoformat(), 'status': status, 'checks': checks,
        'not_applicable_fields': [deepcopy(n) for n in graph['necessities'] if not n['necessary_for_conclusion']],
        'rights_report': deepcopy(rights_report), 'reuse_receipts': deepcopy(list(reuse_receipts)),
        'cost': estimate_cost(snapshot, classification, max_claims=max_claims,
            counter_max_claims=counter_max_claims, resume_margin_phases=resume_margin_phases,
            reuse_receipts=reuse_receipts, reuse_validator=reuse_validator),
        'no_provider_call': True, 'received': False,
        'limits': ['ready admet seulement une vérification indépendante bornée.',
            'not_applicable ne vaut ni PASS, ni absence prouvée, ni conclusion favorable.']}
    report['preflight_digest'] = sha256_digest(report)
    return report


def build_preflight(snapshot, classification, *, render_mode='private_review', rights_report=None,
                    as_of=None, max_claims=32, counter_max_claims=8, resume_margin_phases=2,
                    reuse_receipts=(), reuse_validator=None, source_root=None, received_sources=None):
    validate_classification(snapshot, classification, source_root=source_root, received_sources=received_sources)
    if render_mode not in RENDER_MODES:
        raise ValidationError('invalid_preflight_render_mode')
    day = as_of or timezone.localdate()
    if type(day) is not date:
        raise ValidationError('preflight_as_of_date_required')
    if classification.get('contract') == 'dv.proof-classification/2':
        return _build_dependency_preflight(snapshot, classification, render_mode=render_mode,
            rights_report=rights_report, day=day, max_claims=max_claims,
            counter_max_claims=counter_max_claims, resume_margin_phases=resume_margin_phases,
            reuse_receipts=reuse_receipts, reuse_validator=reuse_validator)
    payload = snapshot['content'].get('public_payload', {})
    if not isinstance(payload, dict):
        raise ValidationError('preflight_public_payload_object_required')
    compare = snapshot.get('kind') in {'speech_vote', 'speech_vote_comparison', 'discourse_vote', 'comparison'} or 'evidence' in payload
    evidence = payload.get('evidence', [])
    evidence = evidence if isinstance(evidence, list) else [evidence]
    present = [e for e in evidence if isinstance(e, dict) and _source(snapshot, e.get('source_version'))]
    source_ok = not compare or bool(present)
    exact_ok = not compare or (bool(present) and all(
        type(e.get('exact_quote')) is str and bool(e['exact_quote'].strip())
        and type(e.get('locator')) is str and bool(e['locator'].strip())
        and any(e['exact_quote'] in p['exact_text'] and e['locator'] == p['locator']
                for p in _source(snapshot, e['source_version']).get('passages', [])) for e in present))
    catalogue = payload.get('catalogue', {})
    catalogue = catalogue if isinstance(catalogue, dict) else {}
    acts = payload.get('acts', [])
    acts = acts if isinstance(acts, list) else []
    included = [a for a in acts if isinstance(a, dict) and a.get('included') is True]
    scrutiny_map = {s.get('scrutin_id', s.get('scrutiny_id')): (i, s)
                    for i, s in enumerate(catalogue.get('scrutinies', [])) if isinstance(s, dict)}
    ids = [a.get('scrutiny_id') for a in included]
    vote_ok = bool(ids) and len(ids) == len(set(ids)) and all(type(x) is str and x in scrutiny_map for x in ids)
    if snapshot.get('kind') == 'official_vote' and not ids:
        ids = [payload.get('scrutin_id')]
        vote_ok = all(type(x) is str and x in scrutiny_map for x in ids)
    object_ok, version_ok = vote_ok, vote_ok
    for identifier in ids:
        index, vote = scrutiny_map.get(identifier, (None, {}))
        object_ok = object_ok and bool(vote.get('procedure')) and bool(vote.get('official_title')) and vote.get('object_state', 'known') == 'known'
        # An URL or date alone does not identify the text actually submitted.
        text_id = vote.get('submitted_text_id', vote.get('text_version_id'))
        texts = [t for t in catalogue.get('text_versions', []) if isinstance(t, dict) and t.get('id') == text_id]
        version_ok = version_ok and vote.get('text_reference_role') == 'submitted' and len(texts) == 1 and _source(snapshot, texts[0].get('source_version')) is not None
        if index is not None:
            object_ok = object_ok and _proven(classification, f'/public_payload/catalogue/scrutinies/{index}/scrutin_id')
    actors = payload.get('actors', {})
    actors = actors if isinstance(actors, dict) else {}
    speaker = actors.get('speaker', payload.get('attribution', {}))
    compared = actors.get('compared_actor', {})
    speaker = speaker if isinstance(speaker, dict) else {}
    compared = compared if isinstance(compared, dict) else {}
    scope = actors.get('scope', payload.get('compared_actor_scope'))
    person_id = compared.get('person_id') or speaker.get('person_id')
    represented = actors.get('represented_entity', {})
    represented = represented if isinstance(represented, dict) else {}
    group_id = compared.get('organization_id') or represented.get('organization_id')
    positions = catalogue.get('vote_observations', [])
    def actor_matches(position, index):
        prefix = f'/public_payload/catalogue/vote_observations/{index}/'
        if scope == 'collective':
            field = 'affiliation_at_event_id' if 'affiliation_at_event_id' in position else 'organization_id'
            return bool(group_id and position.get(field) == group_id and _proven(classification, prefix + field))
        if scope in {'government', 'party'}:
            return False
        return bool(person_id and position.get('person_id') == person_id)
    linked = bool(ids) and all(any(isinstance(o, dict) and o.get('scrutiny_id') == sid
        and actor_matches(o, i)
        and _source(snapshot, o.get('source_version')) is not None
        and _proven(classification, f'/public_payload/catalogue/vote_observations/{i}/person_id')
        for i, o in enumerate(positions)) for sid in ids)
    personal = payload.get('comparison_mode') == 'personal_behavior' or scope == 'personal'
    author_mismatch = personal and (not speaker.get('person_id') or not compared.get('person_id')
                                   or speaker['person_id'] != compared['person_id']
                                   or actors.get('scope') == 'government')
    unresolved = [c for c in classification['claims'] if c['necessary_for_conclusion']
                  and (c['layer'] == 'unknown' or (c['layer'] == 'deterministic' and c.get('provenance_status') != 'proven'))]
    routed_ids = {c['claim_id'] for c in classification['claims']}
    unresolved.extend(u for u in classification['unknowns']
                      if u['necessary_for_conclusion'] and u['claim_id'] not in routed_ids)
    affiliation_bounds = [c for c in classification['claims'] if re.fullmatch(r'/public_payload/catalogue/affiliations/\d+/(starts_on|ends_on)', c['location'])
                          and c['layer'] != 'deterministic']
    rights_ok, rights_missing, rights_reason = _rights(snapshot, rights_report, render_mode, day)
    checks = [
        _check(REASONS[0], source_ok, 'Source figée de la déclaration présente.' if source_ok else 'Source reçue de déclaration absente.', ['/public_payload/evidence'], missing=True),
        _check(REASONS[1], exact_ok, 'Citation exacte et locator liés au passage reçu.' if exact_ok else 'Le passage exact/locator est absent ou ne correspond pas.', ['/public_payload/evidence'], missing=not present),
        _check(REASONS[2], vote_ok, 'Identifiants exacts des actes reliés au catalogue.' if vote_ok else 'Scrutin exact non identifié sans ambiguïté.', ['/public_payload/acts'], missing=not ids),
        _check(REASONS[3], object_ok, 'Identité prouvée et objet/procédure nommés.' if object_ok else 'Objet/identité de vote non établi par les certificats requis.', ['/public_payload/catalogue/scrutinies'], missing=True),
        _check(REASONS[4], linked, 'Position nominative reçue liée à l’acte pertinent.' if linked else 'Personne/groupe non relié par preuve à la date et à l’acte.', ['/public_payload/actors', '/public_payload/catalogue/vote_observations'], missing=True),
        _check(REASONS[5], version_ok, 'Version exacte soumise identifiée.' if version_ok else 'Version du texte soumis nécessaire non établie ; une URL ne suffit pas.', ['/public_payload/catalogue/text_versions'], missing=True),
        _check(REASONS[6], not author_mismatch, 'Portées et auteurs distingués.' if not author_mismatch else 'Auteurs/capacités différents présentés comme comportement personnel.', ['/public_payload/actors']),
        _check(REASONS[7], not unresolved and not affiliation_bounds, 'Aucun inconnu nécessaire à la conclusion dissimulé.' if not unresolved and not affiliation_bounds else 'Inconnus nécessaires ou bornes d’affiliation sans preuve empêchent cette conclusion.', [c['location'] for c in unresolved + affiliation_bounds], missing=True),
        _check(REASONS[8], rights_ok, rights_reason, ['/public_payload/evidence'], missing=rights_missing),
    ]
    status = 'rejected' if any(c['status'] == 'reject' for c in checks) else ('hold' if any(c['status'] == 'hold' for c in checks) else 'ready')
    report = {'contract': CONTRACT, 'fingerprint': sha256_digest(snapshot),
              'classification_digest': classification['classification_digest'],
              'render_mode': render_mode, 'as_of': day.isoformat(), 'status': status,
              'checks': checks, 'rights_report': deepcopy(rights_report), 'reuse_receipts': deepcopy(list(reuse_receipts)),
              'cost': estimate_cost(snapshot, classification, max_claims=max_claims, counter_max_claims=counter_max_claims,
                resume_margin_phases=resume_margin_phases, reuse_receipts=reuse_receipts, reuse_validator=reuse_validator),
              'no_provider_call': True, 'received': False}
    report['preflight_digest'] = sha256_digest(report)
    return report


def validate_preflight(snapshot, classification, report, *, source_root=None, received_sources=None, reuse_validator=None):
    expected_contract = CONTRACT_V2 if snapshot.get('conclusion') else CONTRACT
    if not isinstance(report, dict) or report.get('contract') != expected_contract:
        raise ValidationError('preflight_contract_required')
    try:
        day = date.fromisoformat(report['as_of'])
        cost = report['cost']
        rebuilt = build_preflight(snapshot, classification, render_mode=report['render_mode'],
            rights_report=report['rights_report'], as_of=day,
            max_claims=cost['max_claims_per_phase'], counter_max_claims=cost['counter_max_claims_per_phase'],
            resume_margin_phases=cost['resume_margin_phases'], source_root=source_root,
            received_sources=received_sources, reuse_receipts=report.get('reuse_receipts', []), reuse_validator=reuse_validator)
    except (KeyError, TypeError, ValueError):
        raise ValidationError('preflight_structure_invalid') from None
    if rebuilt != report:
        raise ValidationError('preflight_content_or_cost_changed')
    return report


def require_received_preflight(snapshot, classification, report, artifact, *, source_root=None, received_sources=None, reuse_validator=None):
    validate_preflight(snapshot, classification, report, source_root=source_root, received_sources=received_sources, reuse_validator=reuse_validator)
    if (report['status'] != 'ready' or artifact is None or artifact.kind != 'preflight'
            or artifact.revision_fingerprint != report['fingerprint']
            or artifact.payload_digest != sha256_digest(report) or artifact.payload != report):
        raise ValidationError('verification_preflight_not_received_or_ready')
    require_current_admissibility(snapshot, report)
    return report


def require_current_admissibility(snapshot, report):
    """Receive history unchanged; recheck time-sensitive admission on launch/read."""
    if report.get('status') != 'ready' or report.get('fingerprint') != sha256_digest(snapshot):
        raise ValidationError('verification_preflight_not_current_or_ready')
    rights_ok, _ = current_preflight_rights(snapshot, report)
    if not rights_ok:
        raise ValidationError('verification_preflight_current_rights_unqualified_or_expired')
    return True


def current_preflight_rights(snapshot, report):
    """Current bool/reason for gates; the received historical digest is retained."""
    qualified, _, reason = _rights(snapshot, report.get('rights_report'), report.get('render_mode'), timezone.localdate())
    return qualified, reason
