"""Projection Site depuis la révision vérifiée et revue, jamais depuis un JSON client."""
from datetime import timedelta
from copy import deepcopy
import hashlib
import hmac
import os
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from atelier.models import EditorialDossier, EditorialRevision, VerificationReceipt, VerificationDelivery
from .canonical import canonical_bytes, sha256_digest
from .publication import evaluate_revision
from .verification import require_verifier, snapshot, POLICY

SITE_SCHEMA = 'dv.verification-site/2'
DOCUMENTARY_SCHEMA = 'dv.documentary-snapshot/1'


def _documentary_snapshot(revision, receipt):
    """Signed projection of the exact server input; never accepted from a caller."""
    current = snapshot(revision)
    checked = receipt.job.input_snapshot
    if (current != checked or sha256_digest(checked) != receipt.fingerprint
            or receipt.fingerprint != receipt.job.fingerprint):
        raise ValidationError('site_documentary_snapshot_not_verified')
    from .diffusion import PUBLIC_PROFILES, project_verified_content
    if revision.public_payload.get('public_contract_version') in PUBLIC_PROFILES and 'diffusion_selection' in revision.public_payload:
        return project_verified_content(checked['content'])
    return {'schema': DOCUMENTARY_SCHEMA, 'content': deepcopy(checked['content'])}


def _sign_packet(packet):
    key = os.environ.get('DV_SITE_VERIFICATION_KEY', '').encode()
    if len(key) < 32 or not packet['key_id']: raise ValidationError('dedicated_site_signing_key_absent')
    packet['signature_algorithm'] = 'HMAC-SHA256'
    packet['signature'] = hmac.new(key, canonical_bytes({k: v for k,v in packet.items() if k != 'signature_algorithm'}), hashlib.sha256).hexdigest()
    return packet


@transaction.atomic
def build_site_suspension(revision, *, user, environment, sequence, reason):
    """Restreindre une livraison existante ; ne crée jamais d'approbation."""
    require_verifier(user)
    dossier_id = EditorialRevision.objects.values_list('dossier_id', flat=True).get(pk=revision.pk)
    EditorialDossier.objects.select_for_update().get(pk=dossier_id)
    revision = EditorialRevision.objects.select_for_update().select_related('dossier').get(pk=revision.pk)
    if environment not in {'test', 'private'}: raise ValidationError('site_environment_not_authorized')
    if environment == 'test' and revision.dossier.data_scope != 'synthetic': raise ValidationError('real_dossier_in_test_export')
    if type(sequence) is not int or sequence < 1: raise ValidationError('site_sequence_invalid')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('site_suspension_reason_required')
    ledger = VerificationDelivery.objects.filter(revision__dossier_id=revision.dossier_id, environment=environment)
    existing = ledger.filter(sequence=sequence).first()
    if existing:
        packet = existing.packet
        if packet.get('operation') != 'suspend' or packet.get('payload') != {'reason': reason}:
            raise ValidationError('site_delivery_sequence_conflict')
        from datetime import datetime
        if datetime.fromisoformat(packet['expires_at']) <= timezone.now():
            raise ValidationError('site_delivery_expired_new_sequence_required')
        return packet
    previous = ledger.order_by('-sequence').first()
    if not previous: raise ValidationError('site_suspension_delivery_missing')
    if sequence <= previous.sequence: raise ValidationError('site_delivery_sequence_replay')
    now = timezone.now()
    payload = {'reason': reason}
    packet = {'schema': previous.packet['schema'], 'key_id': os.environ.get('DV_SITE_VERIFICATION_KEY_ID', ''),
        'environment': environment, 'event_id': str(uuid.uuid4()), 'sequence': sequence, 'operation': 'suspend',
        'record_id': revision.dossier.stable_id, 'revision': previous.packet['revision'],
        'issued_at': now.isoformat(), 'expires_at': (now+timedelta(days=1)).isoformat(),
        'payload_sha256': sha256_digest(payload), 'payload': payload, 'verification': None}
    _sign_packet(packet)
    VerificationDelivery.objects.create(revision=revision, receipt=previous.receipt, environment=environment,
        sequence=sequence, mapping_digest=sha256_digest({'operation': 'suspend', 'reason': reason}), packet=packet)
    return packet


@transaction.atomic
def build_site_packet(revision, *, user, environment, sequence, mapping):
    require_verifier(user)
    dossier_id = EditorialRevision.objects.values_list('dossier_id', flat=True).get(pk=revision.pk)
    EditorialDossier.objects.select_for_update().get(pk=dossier_id)
    revision = EditorialRevision.objects.select_for_update().select_related('dossier').get(pk=revision.pk)
    if environment not in {'test', 'private'}: raise ValidationError('site_environment_not_authorized')
    if environment == 'test' and revision.dossier.data_scope != 'synthetic': raise ValidationError('real_dossier_in_test_export')
    allowed = evaluate_revision(revision, environment)
    if not allowed.allowed: raise ValidationError('site_export_refused:' + allowed.reason)
    if not revision.verification_required: raise ValidationError('verification_required_for_new_site_contract')
    if type(sequence) is not int or sequence < 1: raise ValidationError('site_sequence_invalid')
    from .proof_architecture import assessment_for_revision
    architecture = assessment_for_revision(revision)
    if architecture is not None:
        receipt = VerificationReceipt.objects.select_related('attempt', 'job').filter(
            pk=architecture.get('native_semantic', {}).get('receipt_id'), job__revision=revision,
            fingerprint=sha256_digest(snapshot(revision))).first()
    else:
        receipt = VerificationReceipt.objects.select_related('attempt', 'job').filter(job__revision=revision,
        job__fingerprint=sha256_digest(snapshot(revision)), job__state='completed', decision='supported').order_by('-job_id').first()
    if receipt is None: raise ValidationError('current_verification_receipt_required')
    documentary = _documentary_snapshot(revision, receipt)
    from .diffusion import SNAPSHOT_SCHEMA, SITE_SCHEMA as DIFFUSION_SITE_SCHEMA
    dissemination = documentary['schema'] == SNAPSHOT_SCHEMA
    if not isinstance(mapping, dict) or set(mapping) - {'sources', 'person_external_id', 'related_scrutin_external_id'}:
        raise ValidationError('site_mapping_invalid')
    data = revision.public_payload
    person = mapping.get('person_external_id', '')
    scrutiny = mapping.get('related_scrutin_external_id', '')
    verified_person = data.get('attribution', {}).get('person_id')
    verified_scrutinies = {act.get('scrutiny_id') for act in data.get('acts', []) if isinstance(act, dict)}
    if (not isinstance(person, str) or not isinstance(scrutiny, str)
            or (person and person != verified_person) or (scrutiny and scrutiny not in verified_scrutinies)):
        raise ValidationError('site_identity_not_in_verified_revision')
    source_versions = []
    for ref in revision.source_snapshot:
        target = mapping.get('sources', {}).get(ref['source_id'])
        if not isinstance(target, dict) or set(target) != {'source_id', 'version_id', 'sha256'} or target['sha256'] != ref['sha256']:
            raise ValidationError('explicit_site_source_mapping_required')
        if architecture is not None and target != {k: ref[k] for k in ('source_id', 'version_id', 'sha256')}:
            raise ValidationError('proof_architecture_source_alias_refused')
        source_versions.append(target)
    if architecture is not None:
        if not dissemination:
            raise ValidationError('proof_architecture_requires_diffusion_projection')
        from .proof_architecture_export import public_summary
        documentary['content']['proof_architecture'] = public_summary(architecture, sha256_digest(source_versions))
    existing = VerificationDelivery.objects.filter(revision=revision, environment=environment, sequence=sequence).first()
    if existing:
        if existing.mapping_digest != sha256_digest(mapping) or existing.receipt_id != receipt.pk:
            raise ValidationError('site_delivery_sequence_conflict')
        from datetime import datetime
        if datetime.fromisoformat(existing.packet['expires_at']) <= timezone.now():
            raise ValidationError('site_delivery_expired_new_sequence_required')
        if architecture is not None:
            previous_document = existing.packet.get('payload', {}).get('diffusion_snapshot', {})
            if previous_document != documentary:
                raise ValidationError('site_delivery_assessment_changed_new_sequence_required')
        return existing.packet
    previous = VerificationDelivery.objects.filter(revision__dossier_id=revision.dossier_id, environment=environment).order_by('-sequence').first()
    if previous and sequence <= previous.sequence: raise ValidationError('site_delivery_sequence_replay')
    payload = {'slug': (slugify(revision.dossier.stable_id)[:37] + '-' + sha256_digest(revision.dossier.stable_id)[:12]),
        'title': revision.title, 'statement_exact': data.get('declaration_quote', ''),
        'statement_attribution': data.get('attribution', {}).get('speaker_label') or data.get('attribution', {}).get('name', ''),
        'finding': revision.short_answer, 'reserve': revision.main_caveat,
        # v3 sends only the reviewed dissemination projection. The complete
        # private working snapshot remains in the Atelier and in its receipt.
        'evidence': revision.reasoning,
        ('diffusion_snapshot' if dissemination else 'documentary_snapshot'): documentary,
        'is_test': revision.dossier.data_scope == 'synthetic',
        'person_external_id': mapping.get('person_external_id', ''),
        'related_scrutin_external_id': mapping.get('related_scrutin_external_id', ''), 'source_versions': source_versions}
    now = timezone.now()
    packet = {'schema': ('dv.verification-site/5' if architecture.get('schema') == 'dv.proof-architecture/2' else 'dv.verification-site/4') if architecture is not None else DIFFUSION_SITE_SCHEMA if dissemination else SITE_SCHEMA, 'key_id': os.environ.get('DV_SITE_VERIFICATION_KEY_ID', ''),
        'environment': environment, 'event_id': str(uuid.uuid4()), 'sequence': sequence, 'operation': 'upsert',
        'record_id': revision.dossier.stable_id, 'revision': str(revision.revision), 'issued_at': now.isoformat(),
        'expires_at': (now+timedelta(days=1)).isoformat(), 'payload_sha256': sha256_digest(payload), 'payload': payload,
        'verification': {'policy': POLICY, 'receipt_id': str(receipt.pk), ('private_fingerprint' if dissemination else 'fingerprint'): receipt.fingerprint,
            'result_digest': receipt.result_digest, 'decision': architecture['documentary_status'] if architecture is not None else 'supported', 'provider': receipt.attempt.provider,
            'source_binding_sha256': sha256_digest(source_versions),
            ('diffusion_sha256' if dissemination else 'documentary_sha256'): sha256_digest(documentary)}}
    _sign_packet(packet)
    VerificationDelivery.objects.create(revision=revision, receipt=receipt, environment=environment,
        sequence=sequence, mapping_digest=sha256_digest(mapping), packet=packet)
    return packet
