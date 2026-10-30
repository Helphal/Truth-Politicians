"""Received proof architecture; no inference, editorial signature or publication.

An assessment is rebuilt from received originals and native receipts on read.
Caller-supplied status flags never qualify a semantic or human gate.
"""
from copy import deepcopy
from types import SimpleNamespace

from django.core.exceptions import ValidationError, PermissionDenied, ObjectDoesNotExist
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from atelier.models import ProofArtifact, VerificationReceipt
from .canonical import sha256_digest
from . import verification as v
from .proof_classification import classify_snapshot, validate_classification
from .provenance import build_provenance_report, provenance_for_revision
from .verification_preflight import (build_preflight, validate_preflight,
    require_received_preflight, require_current_admissibility)
from .verification_counterevidence import (
    scan_counterevidence, validate_counterevidence, ADVERSARIAL_RESPONSE_V3)

SCHEMA = 'dv.proof-architecture/1'
DEPENDENCY_SCHEMA = 'dv.proof-architecture/2'
GATES = ('deterministic_provenance_complete', 'semantic_verification_complete',
         'counterevidence_complete', 'eligible_for_human_review',
         'human_reviewed_exact_revision', 'rights_qualified', 'publishable')


def architecture_required(revision):
    return (bool(revision.conclusion_contract)
            or revision.public_payload.get('public_contract_version') == 'dv.content/0.7.0-conclusion-draft.1'
            or (revision.dossier.data_scope == 'real' and revision.dossier.content_kind == 'speech_vote')
            or ProofArtifact.objects.filter(revision=revision).exists())


def _assessment_schema(revision):
    return DEPENDENCY_SCHEMA if revision.conclusion_contract else SCHEMA


def documentary_outcome_eligible(report):
    """A documented non-comparability is a distinct closed conclusion type.

    This helper never completes a gate and never upgrades a legacy negative.
    """
    if report.get('documentary_status') == 'supported':
        return True
    return (report.get('schema') == DEPENDENCY_SCHEMA
            and report.get('documentary_status') == 'not_comparable'
            and report.get('conclusion', {}).get('archetype') ==
                'not_comparable_different_actor_or_capacity')


def received_rights_for_render(revision, render_mode):
    """Current qualified source/version/usage receipts, never a global licence."""
    from .rights_qualification import validate_rights_report
    value = v.snapshot(revision)
    candidates = ProofArtifact.objects.filter(revision=revision, kind='rights').order_by('-pk')
    for artifact in candidates:
        report = artifact.payload
        if report.get('render_mode') != render_mode:
            continue
        try:
            artifact.validate_binding(value)
            validate_rights_report(value, report)
            if report.get('usages') and all(row.get('status') == 'compatible' for row in report['usages']):
                return report
        except (ValidationError, TypeError, KeyError, ValueError):
            continue
    return None


def _latest(revision, kind, schema=None):
    query = ProofArtifact.objects.filter(revision=revision, kind=kind)
    if schema:
        query = query.filter(schema=schema)
    return query.order_by('-pk').first()


def _receive(revision, kind, payload, *, actor_id=None):
    snapshot = v.snapshot(revision)
    fingerprint = sha256_digest(snapshot)
    declared = payload.get('fingerprint', payload.get('revision_fingerprint'))
    if declared != fingerprint:
        raise ValidationError('proof_receipt_revision_changed')
    schema = payload.get('schema', payload.get('contract')) or payload.get('proof_architecture', {}).get('contract')
    digest = sha256_digest(payload)
    existing = ProofArtifact.objects.filter(revision=revision, kind=kind, payload_digest=digest).first()
    if existing:
        existing.validate_binding(snapshot)
        return existing
    return ProofArtifact.objects.create(revision=revision, revision_fingerprint=fingerprint,
        kind=kind, schema=schema, payload=deepcopy(payload), payload_digest=digest, actor_id=actor_id)


@transaction.atomic
def receive_preflight(revision, classification, report, *, actor_id=None):
    value = v.snapshot(revision)
    validate_classification(value, classification)
    validate_preflight(value, classification, report)
    if value.get('conclusion'):
        from .conclusion_dependencies import validate_dependency_graph
        graph = classification['dependency_graph']
        validate_dependency_graph(value, classification, graph)
        _receive(revision, 'dependency_graph', graph, actor_id=actor_id)
        if report.get('rights_report'):
            from .rights_qualification import validate_rights_report
            validate_rights_report(value, report['rights_report'])
            _receive(revision, 'rights', report['rights_report'], actor_id=actor_id)
    return _receive(revision, 'preflight', report, actor_id=actor_id)


def receive_layered_plan(revision, plan, *, actor_id):
    from .verification_layered_plan import validate_plan
    validate_plan(plan)
    require_plan_preflight(revision, plan)
    return _receive(revision, 'preflight', plan, actor_id=actor_id)


def require_plan_preflight(revision, plan):
    architecture = plan.get('proof_architecture')
    if not architecture:
        # Compatibility is limited to explicitly synthetic regression tests.
        if revision.dossier.data_scope == 'synthetic' and not architecture_required(revision):
            return None
        raise ValidationError('real_plan_requires_received_layered_preflight')
    report = architecture['preflight']
    if revision.conclusion_contract:
        from .rights_qualification import REPORT_CONTRACT, validate_rights_report
        rights = report.get('rights_report')
        if not rights:
            raise ValidationError('rights_qualification_required_before_allocation')
        validate_rights_report(v.snapshot(revision), rights)
        received_rights = ProofArtifact.objects.filter(revision=revision, kind='rights',
            schema=REPORT_CONTRACT, payload_digest=sha256_digest(rights)).first()
        if received_rights is None or received_rights.payload != rights:
            raise ValidationError('rights_qualification_not_received')
        received_rights.validate_binding(v.snapshot(revision))
        graph = architecture['classification']['dependency_graph']
        received_graph = ProofArtifact.objects.filter(revision=revision,
            kind='dependency_graph', payload_digest=sha256_digest(graph)).first()
        if received_graph is None:
            raise ValidationError('dependency_graph_not_received')
        received_graph.validate_binding(v.snapshot(revision))
    artifact = ProofArtifact.objects.filter(revision=revision, kind='preflight',
        schema=report.get('contract', report.get('schema')), payload_digest=sha256_digest(report)).first()
    if artifact:
        artifact.validate_binding(v.snapshot(revision))
    return require_received_preflight(v.snapshot(revision), architecture['classification'], report, artifact)


def received_plan(job, result=None):
    from .verification_layered_plan import validate_plan, validate_result
    candidates = ProofArtifact.objects.filter(revision_id=job.revision_id, kind='preflight',
        schema__in=['dv.layered-plan/1', 'dv.layered-plan/2'], revision_fingerprint=job.fingerprint).order_by('-pk')
    for artifact in candidates:
        plan = artifact.payload
        if plan.get('job_id') != job.pk or plan.get('job_key') != job.key:
            continue
        artifact.validate_binding(job.input_snapshot)
        validate_plan(plan)
        require_plan_preflight(job.revision, plan)
        if result is not None:
            validate_result(plan, result)
        return plan
    raise ValidationError('layered_plan_receipt_missing_or_changed')


def _native_semantics(revision, classification):
    fingerprint = sha256_digest(v.snapshot(revision))
    receipt = VerificationReceipt.objects.filter(job__revision=revision,
        fingerprint=fingerprint).select_related('attempt', 'job').order_by('-pk').first()
    if not receipt:
        return None
    attempt, job = receipt.attempt, receipt.job
    if (attempt.job_id != job.pk or not attempt.finished_at or attempt.stage != 'completed'
            or receipt.result_digest != sha256_digest(attempt.result)
            or attempt.result_digest != receipt.result_digest or not v._current(job)):
        raise ValidationError('architecture_native_receipt_corrupt')
    from django.contrib.auth import get_user_model
    actor = get_user_model().objects.defer('password').get(pk=attempt.actor_id)
    v.require_verifier(actor)
    if actor.pk == revision.created_by_id:
        raise PermissionDenied('writer_cannot_verify_own_revision')
    if attempt.provider == 'synthetic' and revision.dossier.data_scope != 'synthetic':
        raise ValidationError('synthetic_verification_on_real_content')
    if attempt.result.get('contract') in {'dv.layered-result/1', 'dv.layered-result/2'}:
        from .verification_layered_plan import semantic_result
        plan = received_plan(job, attempt.result)
        result = semantic_result(plan, attempt.result)
    else:
        v._check_result(job, attempt.result)  # Negative but intact is a valid received judgment.
        result = deepcopy(attempt.result)
    wanted = {c['claim_id'] for c in classification['claims'] if c['layer'] == 'semantic'
              and c.get('necessary_for_conclusion', True)}
    judgments = [c for c in result['claims'] if c['id'] in wanted]
    if {c['id'] for c in judgments} != wanted or len(judgments) != len(wanted):
        raise ValidationError('architecture_semantic_coverage_incomplete')
    subset = deepcopy(result)
    subset['claims'] = judgments
    native = {'schema': 'dv.native-semantic-layer/1', 'fingerprint': fingerprint,
        'receipt_id': receipt.pk, 'attempt_id': attempt.pk, 'result_digest': receipt.result_digest,
        'actor_id': actor.pk, 'execution_id': attempt.execution_id, 'provider': attempt.provider,
        'model': attempt.model, 'effort': attempt.effort, 'result': subset,
        'result_contract': attempt.result.get('contract'),
        'historical_documentary_decision': receipt.decision,
        'generation_actor_id': revision.created_by_id}
    return native


def _gate(complete, reason, *, blocked=False):
    return {'status': 'complete' if complete else ('blocked' if blocked else 'pending'), 'reason': reason}


def _core(revision, classification, preflight, counter, *, verified_at):
    value = v.snapshot(revision)
    validate_classification(value, classification)
    validate_preflight(value, classification, preflight)
    if revision.conclusion_contract:
        from .conclusion_dependencies import validate_dependency_graph
        graph = classification['dependency_graph']
        validate_dependency_graph(value, classification, graph)
        received_graph = ProofArtifact.objects.filter(revision=revision,
            kind='dependency_graph', payload_digest=sha256_digest(graph)).first()
        if received_graph is None:
            raise ValidationError('dependency_graph_not_received')
        received_graph.validate_binding(value)
        if preflight.get('rights_report'):
            from .rights_qualification import validate_rights_report
            rights = preflight['rights_report']
            validate_rights_report(value, rights)
            received_rights = ProofArtifact.objects.filter(revision=revision,
                kind='rights', payload_digest=sha256_digest(rights)).first()
            if received_rights is None:
                raise ValidationError('rights_qualification_not_received')
            received_rights.validate_binding(value)
    try:
        require_current_admissibility(value, preflight)
        preflight_current = {'status': 'ready', 'reason': 'Préflight et droits encore recevables au jour de lecture.'}
    except ValidationError as error:
        preflight_current = {'status': 'blocked', 'reason': error.messages[0]}
    native = _native_semantics(revision, classification)
    first = native['result'] if native else None
    validate_counterevidence(value, counter, first)
    from .verification_counterevidence_executor import received_counter_for_revision
    independent = received_counter_for_revision(revision, classification, native)
    claims = classification['claims']
    deterministic = [c for c in claims if c['layer'] == 'deterministic'
                     and c.get('necessary_for_conclusion', True)]
    provenance_ok = bool(deterministic) and all(c.get('provenance_status') == 'proven' for c in deterministic)
    semantic_ok = native is not None or not any(c['layer'] == 'semantic' and c.get('necessary_for_conclusion', True) for c in claims)
    judgments = first['claims'] if first else []
    objections = [deepcopy(c) for c in judgments if c['verdict'] != 'ETAYE_DANS_PERIMETRE'
                  or c['against'] or not v.action_satisfied(c)]
    objections.extend(deepcopy(row) for row in counter['findings']
                      if row.get('conclusion_applicability', {}).get('required', True))
    necessary_unknowns = [c for c in claims if c['layer'] == 'unknown' and c['necessary_for_conclusion']]
    # Null/empty values are excluded from legacy semantic scalars, but their
    # explicitly necessary unknown annotation remains part of the full proof.
    necessary_unknowns.extend(x for x in classification['unknowns']
        if x.get('necessary_for_conclusion', True)
        and x['location'] not in {c['location'] for c in necessary_unknowns})
    # A bounded mechanical search cannot certify semantic independence/completeness.
    counter_ok = bool(independent and independent['summary'].get('counterevidence_complete')
                      and independent.get('runtime_identity_verified'))
    if independent:
        for response in independent['responses']:
            if response.get('contract') == ADVERSARIAL_RESPONSE_V3:
                # V3 native readback already validated the closed keys. Build
                # local projections only; immutable raw receipts stay objects.
                counter_claims = [{'claim_id': cid, **deepcopy(claim)}
                                  for cid, claim in response['claims'].items()]
                counter_axes = [{'axis': axis, **deepcopy(finding)}
                                for axis, finding in response['axes'].items()]
            else:
                counter_claims, counter_axes = response['claims'], response['axes']
            objections.extend(deepcopy(c) for c in counter_claims if c['conclusion'] != 'no_objection')
            objections.extend(deepcopy(a) for a in counter_axes if a['status'] == 'found'
                and (not value.get('conclusion') or classification['dependency_graph']['counterevidence_requirements'][a['axis']]['required']))
    rights_ok = False
    from .revisions import deterministic_control_results
    controls = deterministic_control_results(revision)
    public_rights = next((x for x in preflight['checks'] if x['code'] == 'rights_incompatible_with_render_mode'), None)
    rights_ok = bool(controls.get('reuse', (False, ''))[0] and public_rights and public_rights['status'] == 'pass'
                     and preflight['render_mode'] == 'public_excerpt' and preflight_current['status'] == 'ready')
    historical_negative = bool(native and native['historical_documentary_decision'] == 'not_supported'
                               and native['result_contract'] not in {'dv.layered-result/1', 'dv.layered-result/2'})
    author_mismatch = any(c['code'] == 'different_authors_as_personal_behavior' and c['status'] == 'reject' for c in preflight['checks'])
    if historical_negative:
        documentary = 'not_supported'
    elif author_mismatch:
        documentary = 'not_comparable'
    elif any(c['verdict'] == 'CONTREDIT' for c in judgments):
        documentary = 'contradicted'
    elif objections:
        documentary = 'mixed' if any(c['verdict'] == 'ETAYE_DANS_PERIMETRE' for c in judgments) else 'not_supported'
    elif necessary_unknowns:
        documentary = 'insufficient_evidence'
    else:
        documentary = 'supported' if provenance_ok and semantic_ok and counter_ok and preflight_current['status'] == 'ready' else 'insufficient_evidence'
    documented_noncomparability = (value.get('conclusion', {}).get('archetype') ==
                                   'not_comparable_different_actor_or_capacity')
    if (documented_noncomparability and not historical_negative and provenance_ok
            and semantic_ok and counter_ok and preflight_current['status'] == 'ready'
            and not objections and not necessary_unknowns):
        documentary = 'not_comparable'
    eligible = (documentary == 'supported' or documentary == 'not_comparable' and documented_noncomparability) and provenance_ok and semantic_ok and counter_ok and preflight_current['status'] == 'ready' and not objections and not necessary_unknowns
    gates = {
        'deterministic_provenance_complete': _gate(provenance_ok, 'Certificats recomputés depuis les originaux ; aucune période déduite d’une observation.', blocked=not provenance_ok),
        'semantic_verification_complete': _gate(semantic_ok, 'Jugement natif exact reçu.' if semantic_ok else 'Jugement natif de la couche B absent.'),
        'counterevidence_complete': _gate(counter_ok, 'Contre-preuve native indépendante et phases exactes reçues.' if counter_ok else 'Scan local reçu ; contrôle sémantique adverse indépendant restant nécessaire.'),
        'eligible_for_human_review': _gate(eligible, 'Portes documentaires et préflight requis.', blocked=bool(objections or necessary_unknowns)),
        'human_reviewed_exact_revision': _gate(False, 'Aucun avis humain déduit d’une preuve technique.'),
        'rights_qualified': _gate(rights_ok, 'Contrôles de droits actuels pour les usages publics.', blocked=not rights_ok),
        'publishable': _gate(False, 'Toutes les portes et la revue personnelle exacte sont requises.', blocked=True),
    }
    provenance = classification.get('provenance_report') or {}
    from atelier.models import DocumentVersion
    rights_current = []
    for reference in revision.source_snapshot:
        document = DocumentVersion.objects.filter(source__stable_id=reference.get('source_id'),
            version_id=reference.get('version_id'), sha256=reference.get('sha256')).select_related('source').first()
        rights_current.append({'reference': reference, 'rights_status': document.source.rights_status if document else 'missing',
            'terms_url': document.source.terms_url if document else '',
            'qualified_at': document.source.qualified_at.isoformat() if document and document.source.qualified_at else None})
    if revision.conclusion_contract and preflight.get('rights_report'):
        rights_current = deepcopy(preflight['rights_report'].get('source_qualifications', rights_current))
    unknowns = [{'location': x['location'], 'state': 'ambiguous' if x['unknown_kind'] == 'ambiguous' else 'unknown',
        'explanation': x['motif'], 'necessary_for_conclusion': x.get('necessary_for_conclusion', True)}
        for x in classification['unknowns']]
    editorial = [{'selection_rule_id': x['selection_rule_id'], 'reason': x['motif'],
        'limits': x['limits'], 'included': x['included_objects'], 'excluded': x['excluded_objects'],
        'counter_cases': x['counter_cases'], 'version': x['version']} for x in classification['editorial_rules']]
    report = {'schema': _assessment_schema(revision), 'revision_fingerprint': sha256_digest(value),
        'documentary_status': documentary, 'classification': deepcopy(classification),
        'preflight': deepcopy(preflight), 'counterevidence': deepcopy(counter), 'native_semantic': native,
        'independent_counterevidence': deepcopy(independent),
        'rights_current': rights_current, 'preflight_current': preflight_current,
        'layers': {
            'provenance': {'status': 'complete' if provenance_ok else 'blocked', 'summary': 'Extraction de faits typés depuis les fichiers reçus.', 'certificates': provenance.get('certificates', [])},
            'semantic': {'status': 'complete' if semantic_ok else 'pending', 'summary': 'Sens, attribution, contexte et pertinence restent distincts de l’extraction.', 'claims': judgments},
            'editorial': {'status': 'complete', 'summary': 'Décisions déclarées avec motif et limites ; aucun fait politique exempté.', 'decisions': editorial},
            'unknowns': {'status': 'blocked' if necessary_unknowns else 'complete', 'summary': 'Les inconnus nécessaires empêchent une conclusion.', 'unknowns': unknowns}},
        'gates': gates, 'summary': {'short_answer': revision.short_answer, 'main_caveat': revision.main_caveat,
            'counts': classification['summary'], 'objections': len(objections),
            'essential_proofs': [deepcopy(c) for c in provenance.get('certificates', []) if c['status'] == 'proven'][:5],
            'counter_elements': deepcopy(counter['findings'][:5]), 'uncertainty': deepcopy(unknowns[:5])},
        'objections': objections, 'unknowns': unknowns, 'editorial_rules': editorial, 'verified_at': verified_at}
    if revision.conclusion_contract:
        report['conclusion'] = deepcopy(value['conclusion'])
        report['dependency_graph'] = deepcopy(classification['dependency_graph'])
    return report


@transaction.atomic
def receive_local_assessment(revision, *, rights_report=None, editorial_rules=(), unknowns=(), actor_id=None,
                             render_mode='private_review'):
    value = v.snapshot(revision)
    provenance = provenance_for_revision(revision, persist=True)
    classification = classify_snapshot(value, provenance_report=provenance,
        editorial_rules=editorial_rules, unknowns=unknowns, conclusion=value.get('conclusion'))
    preflight = build_preflight(value, classification, rights_report=rights_report, render_mode=render_mode)
    receive_preflight(revision, classification, preflight, actor_id=actor_id)
    native = _native_semantics(revision, classification)
    counter = scan_counterevidence(value, native['result'] if native else None,
        generation_actor_id=revision.created_by_id,
        first_judgment_actor_id=native['actor_id'] if native else None,
        first_judgment_execution_id=native['execution_id'] if native else None,
        counter_actor_id=actor_id, counter_execution_id='local-bounded-scan')
    _receive(revision, 'counterevidence', counter, actor_id=actor_id)
    report = _core(revision, classification, preflight, counter, verified_at=timezone.now().isoformat())
    return _receive(revision, 'assessment', report, actor_id=actor_id)


def assessment_for_revision(revision, *, include_human=True):
    artifact = _latest(revision, 'assessment', _assessment_schema(revision))
    if artifact is None:
        return None
    try:
        value = v.snapshot(revision)
        artifact.validate_binding(value)
        body = artifact.payload
        rebuilt = _core(revision, body['classification'], body['preflight'], body['counterevidence'],
                        verified_at=body['verified_at'])
        if rebuilt != body:
            raise ValidationError('architecture_received_assessment_changed')
        report = deepcopy(body)
        report['assessment_digest'] = artifact.payload_digest
        if include_human:
            environment = settings.DV_ENV if settings.DV_ENV in {'test', 'private', 'production'} else 'private'
            human = _human_current(revision, report, environment)
            report['gates']['human_reviewed_exact_revision'] = _gate(human,
                'Revue personnelle liée à cette preuve exacte.' if human else 'Aucune revue personnelle exacte recevable.')
            publishable = bool(human and documentary_outcome_eligible(report)
                and all(report['gates'][g]['status'] == 'complete' for g in GATES[:6])
                and _publication_controls_current(revision, environment))
            report['gates']['publishable'] = _gate(publishable,
                'Portes et revue exactes reçues ; aucune publication exécutée.' if publishable else 'Portes documentaires, droits et revue exacte requis.', blocked=not publishable)
        return report
    except (ValidationError, PermissionDenied, ObjectDoesNotExist, KeyError, TypeError, ValueError):
        return None


def _human_current(revision, report, environment):
    from .publication import source_snapshot_digest, _review_authority_binding_valid
    review = revision.human_reviews.filter(environment=environment).select_related('reviewer').order_by('-created_at', '-pk').first()
    if review is None:
        return False
    try:
        profile = review.reviewer.dv_profile
        if (review.revoked or review.decision != 'approve' or review.created_via not in {'human_ui', 'detached_ui'}
                or review.reviewed_digest != revision.snapshot_digest
                or review.evidence_snapshot_digest != source_snapshot_digest(revision)
                or review.proof_architecture_digest != report['assessment_digest']
                or not review.reviewer.is_active or not review.reviewer.has_usable_password()
                or not _review_authority_binding_valid(review, profile, environment)):
            return False
        if review.created_via == 'detached_ui':
            from .detached_review import verify_current_receipt
            return verify_current_receipt(review)
        return True
    except (AttributeError, ObjectDoesNotExist, PermissionDenied, ValidationError):
        return False


def _publication_controls_current(revision, environment):
    from .publication import REQUIRED_CONTROLS, _sources_current
    from .catalogue import binding_check
    if (revision.dossier.suspended or revision.dossier.current_revision != revision.revision
            or sha256_digest(revision.public_payload) != revision.snapshot_digest
            or not _sources_current(revision) or not binding_check(revision)[0]):
        return False
    latest = {}
    for control in revision.controls.filter(environment=environment).order_by('control_type', '-created_at', '-pk'):
        latest.setdefault(control.control_type, control)
    required = REQUIRED_CONTROLS[revision.dossier.content_kind]
    return required <= set(latest) and all(latest[k].passed and latest[k].input_digest == revision.snapshot_digest for k in required)


def documentary_gate(revision):
    report = assessment_for_revision(revision)
    if report is None:
        return False, 'proof_architecture_missing_or_stale'
    if not documentary_outcome_eligible(report):
        return False, 'proof_architecture_documentary_unresolved'
    required = GATES[:4]
    if any(report['gates'][g]['status'] != 'complete' for g in required):
        return False, 'proof_architecture_gates_incomplete'
    return True, 'layered_documentary_proof_received'
