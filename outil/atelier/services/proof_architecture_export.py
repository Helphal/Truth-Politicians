"""Project a received server assessment; no inference and no status promotion."""
from django.core.exceptions import ValidationError
from .proof_architecture import GATES, documentary_outcome_eligible
from .canonical import sha256_digest

def public_summary(assessment, source_binding_sha256):
    if (assessment is None or assessment.get('schema') not in {'dv.proof-architecture/1', 'dv.proof-architecture/2'}
            or not documentary_outcome_eligible(assessment)
            or any(assessment['gates'][g]['status'] != 'complete' for g in GATES)):
        raise ValidationError('site_proof_architecture_not_publishable')
    summary = {'schema': 'dv.proof-architecture-public/1',
        'revision_fingerprint': assessment['revision_fingerprint'],
        'assessment_digest': assessment['assessment_digest'],
        'source_binding_sha256': source_binding_sha256,
        'documentary_status': assessment['documentary_status'],
        'layers': {k: {field: v[field] for field in ('status', 'summary')} for k, v in assessment['layers'].items()},
        'gates': {k: {field: v[field] for field in ('status', 'reason')} for k, v in assessment['gates'].items()},
        'necessary_unknowns': [{k: v[k] for k in ('location', 'state', 'explanation')} for v in assessment['unknowns']],
        'editorial_scope': [{k: v[k] for k in ('selection_rule_id', 'reason', 'limits')} for v in assessment['editorial_rules']],
        'verified_at': assessment['verified_at']}
    if assessment['schema'] == 'dv.proof-architecture/2':
        conclusion = assessment['conclusion']
        summary['schema'] = 'dv.proof-architecture-public/2'
        summary['conclusion'] = {
            'archetype': conclusion['archetype'],
            'temporal_scope': conclusion['temporal_scope']['kind'],
            'dependency_graph_sha256': sha256_digest(assessment['dependency_graph']),
            'comparison_claimed': conclusion['archetype'] not in {
                'not_comparable_different_actor_or_capacity', 'withdrawn_or_not_voted_object'},
        }
        summary['necessary_unknowns'] = [
            {key: original[key] for key in ('location', 'state', 'explanation')}
            for original in assessment['unknowns'] if original.get('necessary_for_conclusion', True)]
        summary['nonblocking_unknowns'] = [
            {key: original[key] for key in ('location', 'state', 'explanation')}
            for original in assessment['unknowns'] if not original.get('necessary_for_conclusion', True)]
    return summary
