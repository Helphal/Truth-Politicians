"""Candidate detached Ed25519 review of ALL substantive content, never signs for a human."""
import base64
import binascii
from copy import deepcopy
from datetime import timedelta
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .canonical import canonical_bytes,sha256_digest

FORMAT='dv.full-review/0.1.0-draft.1'
RACC001_FORMAT='dv.full-review/0.1.0-racc001-draft.1'
DIFFUSION_FORMAT='dv.full-review/0.2.0-diffusion-draft.1'
PROOF_ARCHITECTURE_FORMAT='dv.full-review/0.3.0-proof-architecture-draft.1'
DEPENDENCY_FORMAT='dv.full-review/0.4.0-conclusion-dependencies-draft.1'
DVVERITE_FORMAT='dv.full-review/0.1.0-dvverite-draft.1'
INDICATOR_FORMAT='dv.full-review/0.1.0-draft.2'
DOMAIN=b'DV-FULL-REVIEW-ED25519-V1\n'
RACC001_PUBLIC_MARKERS=frozenset({
    'actors', 'qualified_declaration', 'qualification_material',
    'declaration_date_precision', 'compared_actor_scope', 'collective_distribution',
})


def uses_racc001_public(public: dict) -> bool:
    return isinstance(public, dict) and any(marker in public for marker in RACC001_PUBLIC_MARKERS)


def full_review_schema_filename(bundle: dict) -> str:
    if bundle.get('format') == DEPENDENCY_FORMAT:
        return 'dv-full-review-0.4.0-conclusion-dependencies-draft.1.schema.json'
    if bundle.get('format') == PROOF_ARCHITECTURE_FORMAT:
        return 'dv-full-review-0.3.0-proof-architecture-draft.1.schema.json'
    if bundle.get('format') == DIFFUSION_FORMAT:
        return 'dv-full-review-0.2.0-diffusion-draft.1.schema.json'
    if bundle.get('format') == DVVERITE_FORMAT:
        return 'dv-full-review-0.1.0-dvverite-draft.1.schema.json'
    if bundle.get('format') == INDICATOR_FORMAT:
        return 'dv-full-review-0.1.0-draft.2.schema.json'
    if bundle.get('format') == RACC001_FORMAT:
        return 'dv-full-review-0.1.0-racc001-draft.1.schema.json'
    return 'dv-full-review-0.1.0-draft.1.schema.json'

def build_review_bundle(revision,*,environment='test',destination='dv-site:test'):
    from atelier.models import EditorialRevision
    revision = EditorialRevision.objects.select_related('dossier').get(pk=revision.pk)
    from .catalogue import binding_check
    if revision.dossier.content_kind!='speech_vote':raise ValidationError('full_review_analysis_required')
    revision.dossier.refresh_from_db()
    if revision.dossier.current_revision!=revision.revision:raise ValidationError('full_review_revision_stale')
    if not binding_check(revision)[0]:raise ValidationError('full_review_source_binding_failed')
    review_format = RACC001_FORMAT if uses_racc001_public(revision.public_payload) else FORMAT
    if 'public_contract_version' in revision.public_payload:
        from .contracts import public_contract_version
        if public_contract_version(revision.public_payload) == 'dv.content/0.4.0-dvverite-draft.1':
            review_format = DVVERITE_FORMAT
        elif public_contract_version(revision.public_payload) == "dv.content/0.5.0-diffusion-draft.1":
            review_format = DIFFUSION_FORMAT
    bundle={'format':review_format,'record_id':revision.dossier.stable_id,'record_revision':revision.revision,
        'environment':environment,'destination':destination,'content_kind':'speech_vote','data_scope':revision.dossier.data_scope,
        'snapshot_digest':revision.snapshot_digest,'public':deepcopy(revision.public_payload),
        'source_versions':deepcopy(revision.source_snapshot),'method_version':revision.method_version}
    from atelier.models import IndicatorContribution
    indicator=IndicatorContribution.objects.filter(revision=revision).first()
    if indicator:
        if sha256_digest(indicator.payload)!=indicator.payload_digest:raise ValidationError('indicator_digest_mismatch')
        if indicator.payload.get('value') in {-1,1} and indicator.payload.get('qualification')!=revision.public_payload.get('result'):
            raise ValidationError('indicator_relationship_disagrees_with_reviewed_result')
        bundle['indicator_candidate']=deepcopy(indicator.payload)
        # Public RACC001 + indicateur : rester sur le schéma RACC001 (champs public étendus).
        # Sinon format indicateur draft.2 (public historique).
        if not uses_racc001_public(revision.public_payload):
            bundle['format']=INDICATOR_FORMAT
    from .proof_architecture import assessment_for_revision, architecture_required
    architecture = assessment_for_revision(revision, include_human=False)
    if architecture is not None:
        bundle['format'] = PROOF_ARCHITECTURE_FORMAT
        bundle['proof_architecture'] = {
            'schema': 'dv.proof-architecture-review-binding/1',
            'revision_fingerprint': architecture['revision_fingerprint'],
            'assessment_digest': architecture['assessment_digest'],
            'classification_digest': architecture['classification']['classification_digest'],
            'preflight_digest': architecture['preflight']['preflight_digest'],
            'counterevidence_digest': architecture['counterevidence']['counterevidence_digest'],
            'documentary_status': architecture['documentary_status']}
        if architecture['schema'] == 'dv.proof-architecture/2':
            bundle['format'] = DEPENDENCY_FORMAT
            bundle['proof_architecture'].update(
                schema='dv.proof-architecture-review-binding/2',
                conclusion_archetype=architecture['conclusion']['archetype'],
                dependency_graph_sha256=sha256_digest(architecture['dependency_graph']))
    elif architecture_required(revision):
        raise ValidationError('full_review_proof_architecture_missing_or_stale')
    if sha256_digest(bundle['public'])!=bundle['snapshot_digest'] or len(canonical_bytes(bundle))>2*1024*1024:raise ValidationError('full_review_bundle_invalid')
    import json
    from pathlib import Path
    from django.conf import settings
    from jsonschema import Draft202012Validator,FormatChecker
    filename = full_review_schema_filename(bundle)
    schema=json.loads((Path(settings.BASE_DIR)/'contracts'/filename).read_text(encoding='utf-8'))
    schema_errors = list(Draft202012Validator(schema,format_checker=FormatChecker()).iter_errors(bundle))
    if schema_errors:
        detail = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<'}:{e.message}" for e in schema_errors[:8]
        )
        raise ValidationError(f"full_review_bundle_schema_invalid:{detail}")
    return bundle

def approval_bytes(bundle,statement):
    """These exact bytes are reviewed/signed by the external human client, not the importer."""
    required={'algorithm','key_id','bundle_sha256','record_id','record_revision','decision','reviewed_at'}
    if not isinstance(statement,dict) or set(statement)!=required:raise ValidationError('full_review_statement_invalid')
    if statement['algorithm']!='ed25519' or statement['decision']!='approve':raise ValidationError('full_review_approval_required')
    if statement['bundle_sha256']!=sha256_digest(bundle) or statement['record_id']!=bundle['record_id'] or statement['record_revision']!=bundle['record_revision']:
        raise ValidationError('full_review_binding_mismatch')
    return DOMAIN+canonical_bytes(statement)

def verify_detached_review(bundle,proof,*,trusted_public_keys,environment,destination,now=None,allow_test_fixtures=False):
    fields={'format','record_id','record_revision','environment','destination','content_kind','data_scope','snapshot_digest','public','source_versions','method_version'}
    if isinstance(bundle, dict) and bundle.get('format') in {PROOF_ARCHITECTURE_FORMAT, DEPENDENCY_FORMAT}:
        fields.add('proof_architecture')
        architecture = bundle.get('proof_architecture', {})
        required = {'schema', 'revision_fingerprint', 'assessment_digest', 'classification_digest',
                    'preflight_digest', 'counterevidence_digest', 'documentary_status'}
        version2 = bundle['format'] == DEPENDENCY_FORMAT
        if version2:
            required |= {'conclusion_archetype', 'dependency_graph_sha256'}
        import re
        if (not isinstance(architecture, dict) or set(architecture) != required
                or architecture['schema'] != ('dv.proof-architecture-review-binding/2' if version2 else 'dv.proof-architecture-review-binding/1')
                or architecture['documentary_status'] not in ({'supported', 'not_comparable'} if version2 else {'supported'})
                or any(not isinstance(architecture[k], str) or not re.fullmatch('[a-f0-9]{64}', architecture[k])
                    for k in required - {'schema', 'documentary_status', 'conclusion_archetype'})):
            raise ValidationError('full_review_proof_architecture_unresolved')
        if version2:
            from .conclusion_dependencies import ARCHETYPES
            noncomparability = architecture['conclusion_archetype'] == 'not_comparable_different_actor_or_capacity'
            if (architecture['conclusion_archetype'] not in ARCHETYPES
                    or architecture['documentary_status'] != ('not_comparable' if noncomparability else 'supported')
                    or noncomparability and bundle.get('public', {}).get('result') != 'not_comparable'):
                raise ValidationError('full_review_conclusion_type_mismatch')
    has_indicator = isinstance(bundle, dict) and 'indicator_candidate' in bundle
    if has_indicator or (isinstance(bundle,dict) and bundle.get('format')==INDICATOR_FORMAT):
        fields.add('indicator_candidate')
        from .indicators import contribution,FIELDS
        indicator=bundle.get('indicator_candidate',{})
        try:
            if contribution({k:indicator[k] for k in FIELDS})!=indicator:raise ValidationError('indicator_derived_value_mismatch')
            if indicator.get('value') in {-1,1} and indicator['qualification']!=bundle.get('public',{}).get('result'):
                raise ValidationError('indicator_relationship_disagrees_with_reviewed_result')
        except (KeyError,TypeError):raise ValidationError('indicator_shape_invalid')
    if not isinstance(bundle,dict) or set(bundle)!=fields or type(bundle.get('record_revision')) is not int or bundle['record_revision']<1 or len(canonical_bytes(bundle))>2*1024*1024:
        raise ValidationError('full_review_bundle_invalid')
    if environment not in {'test','private','production'} or bundle.get('environment')!=environment or bundle.get('destination')!=destination:raise ValidationError('full_review_target_mismatch')
    if bundle.get('format') not in {FORMAT,RACC001_FORMAT,INDICATOR_FORMAT,DVVERITE_FORMAT,DIFFUSION_FORMAT,PROOF_ARCHITECTURE_FORMAT,DEPENDENCY_FORMAT} or bundle.get('content_kind')!='speech_vote' or sha256_digest(bundle.get('public'))!=bundle.get('snapshot_digest'):raise ValidationError('full_review_bundle_invalid')
    if not isinstance(proof,dict) or set(proof)!={'statement','signature'}:raise ValidationError('full_review_proof_invalid')
    statement=proof['statement'];message=approval_bytes(bundle,statement)
    key_id=statement['key_id']
    if not isinstance(key_id,str) or key_id not in trusted_public_keys:raise ValidationError('full_review_key_unknown')
    if key_id.startswith('test-') or bundle.get('data_scope')!='real':
        if environment=='production' or not allow_test_fixtures:raise ValidationError('full_review_test_fixture_rejected')
    stamp=parse_datetime(statement['reviewed_at']) if isinstance(statement['reviewed_at'],str) else None
    now=now or timezone.now()
    if not stamp or timezone.is_naive(stamp) or stamp>now+timedelta(seconds=30):raise ValidationError('full_review_date_invalid')
    try:
        key=Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted_public_keys[key_id],validate=True))
        key.verify(base64.b64decode(proof['signature'],validate=True),message)
    except (ValueError,TypeError,binascii.Error,InvalidSignature) as exc:raise ValidationError('full_review_signature_invalid') from exc
    return {'verified_detached_signature':True,'bundle_sha256':sha256_digest(bundle),'statement_sha256':sha256_digest(statement),
        'record_id':bundle['record_id'],'record_revision':bundle['record_revision'],'key_id':key_id,
        'send_authorized':False,'publication_authorized':False,'site_adoption_required':True}
