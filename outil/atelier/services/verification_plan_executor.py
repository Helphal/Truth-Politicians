"""Explicit allocation, durable bounded phases, exact frozen DV-VERITE plan.

No daemon, automatic provider retry, default budget, synthetic real-data route,
or implicit recovery of a launched effect. The journal is versioned private
runtime data; it is not an editorial review or a publication authorization.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from atelier.models import DocumentVersion, UserProfile, VerificationAttempt, VerificationJob, VerificationReceipt
from .canonical import canonical_bytes, sha256_digest
from . import verification as v
from .verification_executor import CodexCLI, ExecutionFailure, _recovered_output
from . import verification_protocol as p


EXECUTOR = 'dv.plan-executor/1.0.0'
ALLOCATION = 'dv.verification-allocation/1'
MAX_JSON = 64 * 1024 * 1024
TOKEN = re.compile(r'[A-Za-z0-9_./:-]{3,160}')
IMPLEMENTATION = 'short-allocation-lock-immutable-source/1'
TRANSPORTS = {'legacy', 'lossless-v1'}


class ProtocolCLI(CodexCLI):
    """Use the received CLI isolation/supervision; only packet transport changes."""

    def __init__(self, timeout=600, executable=None, transport=None):
        self.transport_mode = transport or 'legacy'
        if self.transport_mode not in TRANSPORTS:
            raise ExecutionFailure('invalid_transport_mode')
        super().__init__(timeout=timeout, executable=executable)
        if self.transport_mode != 'legacy':
            self.identity = {**self.identity, 'transport_mode': self.transport_mode}

    def response_schema(self, stage, packet):
        return p.response_schema(stage, packet)

    def prompt_for(self, stage, packet):
        return _transport_prompt(stage, packet, getattr(self, 'transport_mode', 'legacy'))

    def run(self, stage, packet, directory, check):
        if getattr(self, 'transport_mode', 'legacy') == 'lossless-v1':
            from . import verification_transport as transport
            try:
                transport.write_receipt(stage, packet, directory)
            except transport.TransportFailure as error:
                raise ExecutionFailure(error.code) from None
        return super().run(stage, packet, directory, check)


def runtime_root():
    return Path(getattr(settings, 'DV_VERIFICATION_PLAN_ROOT',
                        Path(settings.RUNTIME_DIR) / 'verification-plans')).absolute()


def _safe(path, root=None):
    path = Path(path).absolute()
    root = Path(root or runtime_root()).absolute()
    resolved = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    if resolved == resolved_root or resolved.is_relative_to(resolved_root):
        for ancestor in (path, *path.parents, root, *root.parents):
            if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
                raise ExecutionFailure('execution_path_link_refused')
    else:
        raise ExecutionFailure('execution_path_outside_owned_root')
    if root == Path(root.anchor):
        raise ExecutionFailure('execution_root_too_broad')
    return resolved


def _write(path, value):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value)
    if len(data) > MAX_JSON:
        raise ExecutionFailure('journal_capture_limit')
    fd, name = tempfile.mkstemp(prefix='.dv-plan-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read(path, *, external=False, max_bytes=MAX_JSON):
    path = Path(path).absolute() if external else _safe(path)
    if external and path.suffix.lower() != '.json':
        raise ExecutionFailure('external_input_must_be_explicit_json')
    if any(ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction())
           for ancestor in (path, *path.parents)):
        raise ExecutionFailure('execution_path_link_refused')
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ExecutionFailure('execution_file_missing_or_oversized')
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError('duplicate_json_member')
            result[name] = value
        return result
    try:
        with path.open('rb') as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ExecutionFailure('execution_file_missing_or_oversized')
        return json.loads(data, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ExecutionFailure('malformed_persisted_json') from None


def _read_bytes(path, max_bytes, *, root=None):
    path = _safe(path, root)
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ExecutionFailure('response_capture_limit')
    with path.open('rb') as stream:
        value = stream.read(max_bytes + 1)
    if len(value) > max_bytes:
        raise ExecutionFailure('response_capture_limit')
    return value


def documentary_root():
    """Dedicated input root; JSON never supplies an evidence-vault path."""
    return Path(getattr(settings, 'DV_VERIFICATION_DOCUMENTARY_ROOT',
                        runtime_root() / 'documentary-inputs')).absolute()


def _documentary_originals(snapshot, bundle):
    """Only exact pinned DB source/version/hash references choose original files."""
    originals = {}
    records = bundle.get('records', []) if isinstance(bundle, dict) else []
    if not isinstance(records, list):
        raise ValidationError('invalid_documentary_records')
    for record in records:
        proof = record.get('proof', {}) if isinstance(record, dict) else {}
        if not isinstance(proof, dict) or proof.get('kind') != 'pinned_original_segment':
            continue
        ref = proof.get('source_version')
        if not isinstance(ref, dict) or ref not in [s['reference'] for s in snapshot['sources']]:
            raise ValidationError('documentary_original_not_pinned')
        digest = ref.get('sha256')
        if digest in originals:
            continue
        document = DocumentVersion.objects.filter(source__stable_id=ref.get('source_id'),
            version_id=ref.get('version_id'), sha256=digest, quarantined=False).first()
        if document is None or not document.original_path:
            raise ValidationError('documentary_original_missing')
        root = Path(settings.DV_EVIDENCE_VAULT_PATH).absolute()
        path = Path(document.original_path)
        path = path if path.is_absolute() else root / path
        originals[digest] = _read_bytes(path, v.MAX_ORIGINAL_BYTES, root=root)
    return originals


def _prepare_documentary(snapshot, path):
    path = _safe(path, documentary_root())
    bundle = _read(path, external=True, max_bytes=p.MAX_DOCUMENTARY_INPUT_BYTES)
    return p.freeze_documentary_input(snapshot, bundle,
        source_originals=_documentary_originals(snapshot, bundle))


def _recheck_documentary(snapshot, annex):
    p.validate_documentary_input(snapshot, annex)
    bundle = p.documentary_bundle(annex)
    checked = p.freeze_documentary_input(snapshot, bundle,
        source_originals=_documentary_originals(snapshot, bundle))
    if checked != annex:
        raise ValidationError('documentary_originals_changed')


@contextmanager
def _lock(path, *, wait_seconds=0):
    """OS releases the exclusive lock on process loss, unlike stale PID markers."""
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    acquired = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + wait_seconds
        while not acquired:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ExecutionFailure('allocation_already_running') from None
                time.sleep(min(.025, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


@contextmanager
def _application_write():
    """Serialize only application writes shared by SQLite jobs, never a provider."""
    with _lock(runtime_root() / 'application-writes' / 'lock', wait_seconds=30):
        yield


def _transport_prompt(stage, packet, mode):
    if mode not in TRANSPORTS:
        raise ExecutionFailure('invalid_transport_mode')
    if mode == 'lossless-v1':
        from . import verification_transport as transport
        try:
            return transport.build_prompt(stage, packet)
        except transport.TransportFailure as error:
            raise ExecutionFailure(error.code) from None
    return p.prompt_for(stage, packet)


def _prompt_binding(stage, packet, mode):
    logical = p.prompt_for(stage, packet).encode('utf-8')
    actual = _transport_prompt(stage, packet, mode).encode('utf-8')
    binding = {'input_sha256': hashlib.sha256(actual).hexdigest(),
               'logical_input_sha256': hashlib.sha256(logical).hexdigest(),
               'logical_packet_sha256': sha256_digest(packet), 'transport_mode': mode}
    if mode == 'lossless-v1':
        from . import verification_transport as transport
        try:
            proof = transport.receipt(stage, packet)
        except transport.TransportFailure as error:
            raise ExecutionFailure(error.code) from None
        if (proof['input_sha256'] != binding['input_sha256']
                or proof['logical_input_sha256'] != binding['logical_input_sha256']
                or proof['logical_packet_sha256'] != binding['logical_packet_sha256']):
            raise ExecutionFailure('transport_prompt_binding_mismatch')
        binding['transport_receipt_sha256'] = sha256_digest(proof)
    return actual, binding


def _current(plan, user):
    v.require_verifier(user)
    if plan['owner_id'] != user.pk:
        raise PermissionDenied('verification_plan_owner')
    job = VerificationJob.objects.select_related('revision__dossier').get(pk=plan['job_id'])
    if user.pk == job.revision.created_by_id:
        raise PermissionDenied('writer_cannot_verify_own_revision')
    if (job.key != plan['job_key'] or job.revision_id != plan['revision_id']
            or job.policy != plan['policy'] or job.fingerprint != plan['fingerprint']
            or job.input_snapshot != plan['snapshot'] or not v._current(job)):
        raise ValidationError('stale_verification_plan')
    if not v.deterministic(job)['passed']:
        raise ValidationError('plan_deterministic_preconditions_failed')
    if 'documentary_annex' in plan:
        _recheck_documentary(job.input_snapshot, plan['documentary_annex'])
    return job


def prepare_plan(job_id, *, user, model='gpt-6-sol', effort='high', limits=None,
                 source_reuse_plan_digest=None, documentary_input_path=None,
                 classification=None, preflight=None, rights_report=None):
    """Only read the job; preparing a plan never reserves budget or starts it."""
    v.require_verifier(user)
    job = VerificationJob.objects.select_related('revision__dossier').get(pk=job_id)
    if user.pk == job.revision.created_by_id:
        raise PermissionDenied('writer_cannot_verify_own_revision')
    if not v._current(job) or not v.deterministic(job)['passed']:
        raise ValidationError('plan_deterministic_preconditions_failed')
    annex = (_prepare_documentary(job.input_snapshot, documentary_input_path)
             if documentary_input_path is not None else None)
    from .proof_architecture import architecture_required
    layered = (classification is not None or preflight is not None
               or job.revision.dossier.data_scope != 'synthetic'
               or architecture_required(job.revision))
    if layered:
        from .proof_classification import classify_snapshot
        from .provenance import build_provenance_report
        from .verification_preflight import build_preflight
        from .proof_architecture import receive_preflight, require_plan_preflight, receive_layered_plan
        if classification is None:
            classification = classify_snapshot(job.input_snapshot,
                provenance_report=build_provenance_report(job.input_snapshot))
        if preflight is None:
            preflight = build_preflight(job.input_snapshot, classification, rights_report=rights_report)
        receive_preflight(job.revision, classification, preflight, actor_id=user.pk)
    plan = p.build_plan(job.input_snapshot, job_id=job.pk, job_key=job.key,
        revision_id=job.revision_id, owner_id=user.pk, model=model, effort=effort,
        limits=limits, source_reuse_plan_digest=source_reuse_plan_digest,
        documentary_annex=annex, **({'classification': classification, 'preflight': preflight} if layered else {}))
    if layered:
        require_plan_preflight(job.revision, plan)
        receive_layered_plan(job.revision, plan, actor_id=user.pk)
    directory = _safe(runtime_root() / 'plans' / plan['plan_digest'])
    path = directory / 'plan.json'
    if path.exists():
        if _read(path) != plan:
            raise ExecutionFailure('frozen_plan_conflict')
    else:
        _write(path, plan)
    return path, plan


def _allocation(path, plan, user):
    if not path:
        raise ExecutionFailure('explicit_unconsumed_allocation_required')
    value = _read(path, external=True, max_bytes=64 * 1024)
    fields = {'contract', 'allocation_id', 'authorization', 'authority_reference',
              'owner_id', 'plan_digests', 'fingerprints', 'job_ids', 'model', 'effort',
              'max_phases', 'expires_at'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['contract'] != ALLOCATION or value['authorization'] != 'explicit_dv_verite'
            or type(value['allocation_id']) is not str or not TOKEN.fullmatch(value['allocation_id'])
            or type(value['authority_reference']) is not str or not value['authority_reference'].strip()
            or value['owner_id'] != user.pk or type(value['owner_id']) is not int
            or type(value['max_phases']) is not int or not 1 <= value['max_phases'] <= 10000):
        raise ExecutionFailure('invalid_explicit_allocation')
    for name, wanted, kind in [('plan_digests', plan['plan_digest'], str),
                               ('fingerprints', plan['fingerprint'], str),
                               ('job_ids', plan['job_id'], int)]:
        items = value[name]
        if (not isinstance(items, list) or not items or any(type(x) is not kind for x in items)
                or len(items) != len(set(items)) or wanted not in items):
            raise ExecutionFailure('allocation_scope_mismatch')
    if value['model'] != plan['model'] or value['effort'] != plan['effort']:
        raise ExecutionFailure('allocation_configuration_mismatch')
    try:
        expiry = datetime.fromisoformat(value['expires_at'])
        if expiry.tzinfo is None or expiry <= timezone.now():
            raise ValueError
    except (ValueError, TypeError):
        raise ExecutionFailure('allocation_expired_or_invalid') from None
    return value


def _ledger(allocation):
    directory = _safe(runtime_root() / 'allocations' / sha256_digest(allocation['allocation_id']))
    allocation_path = directory / 'allocation.json'
    if allocation_path.exists():
        if _read(allocation_path) != allocation:
            raise ExecutionFailure('allocation_identity_or_ceiling_changed')
    else:
        _write(allocation_path, allocation)
    path = directory / 'ledger.json'
    value = _read(path) if path.exists() else {'executor': EXECUTOR,
        'allocation_sha256': sha256_digest(allocation), 'phases': []}
    if (value.get('executor') != EXECUTOR or value.get('allocation_sha256') != sha256_digest(allocation)
            or len(value.get('phases', [])) > allocation['max_phases']):
        raise ExecutionFailure('allocation_ledger_mismatch')
    identifiers = set()
    for phase in value['phases']:
        expected = sha256_digest([allocation['allocation_id'], phase['plan_digest'],
                                  phase['batch_id'], phase['run_number']])
        if (phase['phase_id'] != expected or expected in identifiers
                or phase['owner_id'] != allocation['owner_id']
                or phase['allocation_id'] != allocation['allocation_id']
                or phase['plan_digest'] not in allocation['plan_digests']):
            raise ExecutionFailure('allocation_phase_identity_changed')
        identifiers.add(expected)
    return directory, path, value


def _allocation_guard(allocation, guard):
    if guard is not None and guard() != allocation:
        raise ExecutionFailure('allocation_identity_or_ceiling_changed')


def _allocation_snapshot(allocation, *, guard=None):
    directory = runtime_root() / 'allocations' / sha256_digest(allocation['allocation_id'])
    with _lock(directory / 'lock', wait_seconds=5):
        _allocation_guard(allocation, guard)
        _, path, ledger = _ledger(allocation)
        if not path.exists():
            _write(path, ledger)
        return deepcopy(ledger)


def _reserve_phase(allocation, plan, batch, attempt_id, binding, run_number, *, retry_of=None, guard=None):
    """Charge durable budget under a short lock; no provider runs in this lock."""
    if (plan['owner_id'] != allocation['owner_id'] or plan['plan_digest'] not in allocation['plan_digests']
            or type(attempt_id) is not int or attempt_id < 1 or type(run_number) is not int or run_number < 1):
        raise ExecutionFailure('reservation_scope_changed')
    directory = runtime_root() / 'allocations' / sha256_digest(allocation['allocation_id'])
    with _lock(directory / 'lock', wait_seconds=5):
        _allocation_guard(allocation, guard)
        _, path, ledger = _ledger(allocation)
        if len(ledger['phases']) >= allocation['max_phases']:
            raise ExecutionFailure('allocation_ceiling_reached')
        phase_id = sha256_digest([allocation['allocation_id'], plan['plan_digest'], batch['batch_id'], run_number])
        if any(x['phase_id'] == phase_id for x in ledger['phases']):
            raise ExecutionFailure('allocation_phase_already_reserved')
        phase = {'phase_id': phase_id, 'plan_digest': plan['plan_digest'], 'batch_id': batch['batch_id'],
            'run_number': run_number, 'owner_id': plan['owner_id'], 'attempt_id': attempt_id,
            'allocation_id': allocation['allocation_id'], **binding, 'state': 'reserved',
            'reserved_at': timezone.now().isoformat(),
            'native_retry_policy': 'existing_CLI_no_application_retry', 'retry_of': retry_of,
            'executor_implementation': IMPLEMENTATION}
        ledger['phases'].append(phase)
        _write(path, ledger)
        return deepcopy(phase), len(ledger['phases'])


def _ack_phase(allocation, phase_id, updates, *, guard=None):
    """Reload immediately before ACK; never overwrite another plan's updates."""
    allowed = {'state', 'launched_at', 'raw_sha256', 'translated_sha256', 'mapping_sha256',
               'error_code', 'last_control_error'}
    if not isinstance(updates, dict) or not set(updates) <= allowed:
        raise ExecutionFailure('immutable_phase_update_refused')
    directory = runtime_root() / 'allocations' / sha256_digest(allocation['allocation_id'])
    with _lock(directory / 'lock', wait_seconds=5):
        _allocation_guard(allocation, guard)
        _, path, ledger = _ledger(allocation)
        matches = [x for x in ledger['phases'] if x['phase_id'] == phase_id]
        if len(matches) != 1:
            raise ExecutionFailure('allocated_phase_missing')
        phase = matches[0]
        if phase['state'] == 'accepted':
            if updates.get('state', 'accepted') != 'accepted':
                raise ExecutionFailure('accepted_phase_state_regression')
            # A lost journal ACK may re-present an already accepted phase.
            # Preserve the exact legacy dictionary, including absent optional
            # mapping fields; never enrich an old terminal prefix on replay.
            for name, value in updates.items():
                if name == 'state' or (name == 'mapping_sha256' and name not in phase):
                    continue
                if name not in phase or phase[name] != value:
                    raise ExecutionFailure('accepted_phase_immutable_update')
            return deepcopy(phase), len(ledger['phases'])
        phase.update(deepcopy(updates))
        _write(path, ledger)
        return deepcopy(phase), len(ledger['phases'])


def _state_directory(plan):
    return _safe(runtime_root() / 'executions' / plan['plan_digest'])


def _journal(plan, allocation):
    path = _state_directory(plan) / 'journal.json'
    expected = {'executor': EXECUTOR, 'plan_digest': plan['plan_digest'],
                'owner_id': plan['owner_id'], 'fingerprint': plan['fingerprint']}
    if path.exists():
        value = _read(path)
        if any(value.get(k) != x for k, x in expected.items()):
            raise ExecutionFailure('execution_binding_mismatch')
    else:
        value = {**expected, 'attempt_ids': [], 'batches': {}, 'source_reuse': None,
                 'result': None, 'receipt_id': None}
    value['current_allocation_id'] = allocation['allocation_id']
    return path, value


def _pending(plan, journal, retry_batches):
    known = {b['batch_id'] for b in plan['batches']}
    if not set(retry_batches) <= known:
        raise ExecutionFailure('retry_batch_outside_plan')
    pending = 0
    for batch in plan['batches']:
        if batch['stage'] == 'evidence' and plan['source_reuse_plan_digest']:
            continue
        state = journal['batches'].get(batch['batch_id'], {}).get('state', 'prepared')
        if state == 'accepted':
            if batch['batch_id'] in retry_batches:
                raise ExecutionFailure('received_batch_cannot_be_regenerated')
            continue
        if state in {'launched', 'ambiguous'}:
            # _recover() must settle this before calculating an allocation.
            raise ExecutionFailure('launched_effect_ambiguous_do_not_replay')
        if state in {'refused', 'partial', 'interrupted'} and batch['batch_id'] not in retry_batches:
            raise ExecutionFailure('explicit_new_operation_required_for_failed_batch')
        if state == 'returned':
            continue
        pending += 1
    return pending


def _recover(plan, journal, ledger):
    """Verify returns outside the allocation lock, returning only this plan's ACKs.

    ``ledger`` is an immutable allocation snapshot. The caller reloads under a
    short lock for each returned ACK; no foreign or legacy accepted entry is
    rewritten during expensive packet/native/transport verification.
    """
    acknowledgements = []
    # A ledger reservation may be durable before its plan-journal ACK. Preserve
    # this spent reservation rather than allocate the same phase ID again.
    for phase in ledger['phases']:
        if phase.get('plan_digest') == plan['plan_digest']:
            existing = journal['batches'].get(phase['batch_id'])
            if not existing or phase['run_number'] > existing['run_number']:
                journal['batches'][phase['batch_id']] = deepcopy(phase)
            elif (phase['phase_id'] == existing['phase_id']
                    and existing['state'] == 'reserved'
                    and phase['state'] in {'launched', 'returned', 'accepted'}):
                existing['state'] = 'launched'
    for bid, state in journal['batches'].items():
        if state['state'] not in {'launched', 'reserved', 'returned', 'accepted'}:
            continue
        phase = next((x for x in ledger['phases'] if x['phase_id'] == state['phase_id']), None)
        immutable = ('phase_id', 'plan_digest', 'batch_id', 'run_number', 'owner_id',
                     'attempt_id', 'allocation_id', 'input_sha256', 'logical_input_sha256',
                     'logical_packet_sha256', 'transport_mode', 'transport_receipt_sha256',
                     'runner_identity_sha256', 'return_origin')
        if (not phase or phase['plan_digest'] != plan['plan_digest']
                or state.get('batch_id') != bid
                or any(state.get(k) != phase[k] for k in immutable if k in phase)):
            raise ExecutionFailure('recovery_phase_binding_changed')
        directory = _safe(_state_directory(plan) / bid / str(state['run_number']))
        receipt_path = directory / 'return-receipt.json'
        if state['state'] == 'reserved':
            # Reserved budget remains spent, but no provider launch was recorded.
            state['state'] = 'interrupted'; state['error_code'] = 'reserved_before_launch'
            continue
        if state['state'] == 'launched' and not receipt_path.exists():
            state['state'] = 'ambiguous'; state['error_code'] = 'provider_effect_unknown'
            continue
        receipt = _read(receipt_path)
        raw_path = _safe(directory / 'raw-return.json')
        raw_bytes = _read_bytes(raw_path, plan['limits']['max_output_bytes'])
        if (receipt.get('phase_id') != state['phase_id']
                or receipt.get('plan_digest') != plan['plan_digest']
                or receipt.get('raw_sha256') != hashlib.sha256(raw_bytes).hexdigest()
                or receipt.get('input_sha256') != state['input_sha256']):
            raise ExecutionFailure('persisted_return_binding_mismatch')
        batch = next((x for x in plan['batches'] if x['batch_id'] == bid), None)
        if batch is None:
            raise ExecutionFailure('returned_batch_outside_plan')
        _validate_return_transport(plan, batch['stage'], directory, state, receipt)
        if state['state'] == 'launched':
            state['state'] = 'returned'
            state['raw_sha256'] = receipt['raw_sha256']
        if (phase['state'] not in {'launched', 'returned', 'accepted'}
                or (phase['state'] in {'returned', 'accepted'}
                    and phase.get('raw_sha256') != receipt['raw_sha256'])):
            raise ExecutionFailure('returned_phase_state_changed')
        if state['state'] == 'accepted':
            if (sha256_digest(_read(directory / 'translated.json')) != state['translated_sha256']
                    or sha256_digest(_read(directory / 'mapping.json')) != state['mapping_sha256']):
                raise ExecutionFailure('accepted_output_tampered')
            if phase['state'] != 'accepted':
                acknowledgements.append((state['phase_id'],
                    {'state': 'accepted', 'raw_sha256': receipt['raw_sha256'],
                     'translated_sha256': state['translated_sha256'],
                     'mapping_sha256': state['mapping_sha256']}))
        elif phase['state'] == 'launched':
            # A journal ACK may have survived while the allocation ACK did not.
            # Bind the exact attributed raw hash before an accepted replay.
            acknowledgements.append((state['phase_id'],
                {'state': 'returned', 'raw_sha256': receipt['raw_sha256']}))
    return acknowledgements


def _recover_allocation(plan, journal, allocation, *, guard=None):
    snapshot = _allocation_snapshot(allocation, guard=guard)
    acknowledgements = _recover(plan, journal, snapshot)
    for phase_id, updates in acknowledgements:
        _ack_phase(allocation, phase_id, updates, guard=guard)
    return _allocation_snapshot(allocation, guard=guard)


def _qualified_synthetic_return(plan, state):
    """The adapter route is bound to an already qualified SYNTH DB attempt."""
    if (settings.DV_ENV != 'test' or plan['snapshot'].get('scope') != 'synthetic'
            or plan['model'] != 'SYNTH' or plan['effort'] != 'none'):
        return False
    return (UserProfile.objects.filter(user_id=plan['owner_id'], is_test_identity=True).exists()
        and VerificationAttempt.objects.filter(pk=state['attempt_id'], job_id=plan['job_id'],
            actor_id=plan['owner_id'], provider='synthetic', model='SYNTH', effort='none').exists())


def _validate_return_transport(plan, stage, directory, state, receipt):
    """Explicit wire proof, while retaining attributed legacy returns verbatim."""
    identity = receipt.get('runner_identity')
    origin = receipt.get('raw_origin')
    if origin not in {'provider_response_file', 'SYNTH_adapter'}:
        raise ExecutionFailure('unknown_provider_return_origin')
    if (not isinstance(identity, dict) or identity.get('model') != plan['model']
            or identity.get('model_reasoning_effort') != plan['effort']):
        raise ExecutionFailure('persisted_runner_configuration_changed')
    if ('runner_identity_sha256' in state
            and (state['runner_identity_sha256'] != sha256_digest(identity)
                or receipt.get('runner_identity_sha256') != state['runner_identity_sha256'])):
        raise ExecutionFailure('persisted_runner_identity_changed')
    if 'return_origin' in state and (state['return_origin'] != origin
            or receipt.get('return_origin') != state['return_origin']):
        raise ExecutionFailure('persisted_provider_return_origin_changed')
    if origin == 'SYNTH_adapter' and not _qualified_synthetic_return(plan, state):
        raise ExecutionFailure('synthetic_return_outside_qualified_scope')
    packet = _read(directory / 'packet.json')
    mode = state.get('transport_mode', 'legacy')
    _, binding = _prompt_binding(stage, packet, mode)
    if binding['input_sha256'] != state['input_sha256']:
        raise ExecutionFailure('persisted_actual_prompt_changed')
    for name in ('logical_input_sha256', 'logical_packet_sha256', 'transport_mode'):
        if name in state and (state[name] != binding[name] or receipt.get(name) != state[name]):
            raise ExecutionFailure('persisted_logical_transport_binding_changed')
    if mode == 'lossless-v1':
        from . import verification_transport as transport
        try:
            proof = transport.receipt(stage, packet)
            transport.validate_transport(_read(directory / 'transport-frame.json'), packet)
        except transport.TransportFailure as error:
            raise ExecutionFailure(error.code) from None
        if (_read(directory / 'transport-receipt.json') != proof
                or state.get('transport_receipt_sha256') != sha256_digest(proof)
                or receipt.get('transport_receipt_sha256') != sha256_digest(proof)):
            raise ExecutionFailure('persisted_transport_proof_changed')
    if origin == 'provider_response_file':
        if mode != 'legacy' and identity.get('transport_mode') != mode:
            raise ExecutionFailure('native_transport_identity_changed')
        if _read(directory / 'execution.json').get('stage') != stage:
            raise ExecutionFailure('native_provider_stage_changed')
        native = _recovered_output(directory, state['input_sha256'], identity)
        if (native != _read(directory / 'raw-return.json')
                or hashlib.sha256(_read_bytes(directory / 'response.json', MAX_JSON)).hexdigest()
                != receipt['raw_sha256']):
            raise ExecutionFailure('native_provider_return_changed')


def _store_source_receipt(plan, findings, *, owner_id, attempt_id):
    """The first attributed reading is immutable across normal lease rollovers."""
    path = _state_directory(plan) / 'source-receipt.json'
    value = {'executor': EXECUTOR, 'plan_digest': plan['plan_digest'], 'owner_id': owner_id,
             'source_read_identity': plan['source_read_identity'], 'findings': findings,
             'findings_sha256': sha256_digest(findings), 'attempt_id': attempt_id, 'independent_reading': True}
    if path.exists():
        original = _read(path)
        if (set(original) != set(value) or type(original.get('attempt_id')) is not int
                or original['attempt_id'] < 1
                or any(original[k] != value[k] for k in value if k != 'attempt_id')):
            raise ExecutionFailure('immutable_source_receipt_changed')
    else:
        _write(path, value)
        original = value
    if original['attempt_id'] != attempt_id:
        replay = {'executor': EXECUTOR, 'plan_digest': plan['plan_digest'], 'owner_id': owner_id,
                  'source_read_identity': plan['source_read_identity'],
                  'original_attempt_id': original['attempt_id'], 'binding_attempt_id': attempt_id,
                  'source_receipt_sha256': sha256_digest(original), 'independent_reading': False}
        replay_path = _state_directory(plan) / 'source-bindings' / f'attempt-{attempt_id}.json'
        if replay_path.exists() and _read(replay_path) != replay:
            raise ExecutionFailure('source_lease_binding_changed')
        if not replay_path.exists():
            _write(replay_path, replay)
    return original


def _reuse_sources(plan, journal, user):
    digest = plan['source_reuse_plan_digest']
    if not digest:
        return None
    if type(digest) is not str or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ExecutionFailure('invalid_source_reuse_binding')
    other = p.validate_plan(_read(runtime_root() / 'plans' / digest / 'plan.json'))
    if (other['owner_id'] != user.pk or other['model'] != plan['model'] or other['effort'] != plan['effort']
            or other['source_read_identity'] != plan['source_read_identity']):
        raise ExecutionFailure('incompatible_source_reuse')
    _current(other, user)
    path = runtime_root() / 'executions' / digest / 'source-receipt.json'
    receipt = _read(path)
    if (receipt.get('executor') != EXECUTOR or receipt.get('plan_digest') != digest
            or receipt.get('source_read_identity') != plan['source_read_identity']
            or receipt.get('owner_id') != user.pk or receipt.get('findings_sha256') != sha256_digest(receipt['findings'])):
        raise ExecutionFailure('unattributed_source_reuse')
    other_journal = _read(runtime_root() / 'executions' / digest / 'journal.json')
    if other_journal.get('owner_id') != user.pk:
        raise ExecutionFailure('source_reuse_owner_mismatch')
    other_outputs = {}
    for batch in other['batches']:
        if batch['stage'] == 'evidence':
            state = other_journal['batches'].get(batch['batch_id'], {})
            if state.get('state') != 'accepted':
                raise ExecutionFailure('source_reuse_incomplete_reading')
            base = runtime_root() / 'executions' / digest / batch['batch_id'] / str(state['run_number'])
            _validate_return_transport(other, batch['stage'], base, state, _read(base / 'return-receipt.json'))
            raw = _read(base / 'raw-return.json')
            translated, mapping = p.translate_evidence(other, batch, raw)
            if (state.get('translated_sha256') != sha256_digest(translated)
                    or state.get('mapping_sha256') != sha256_digest(mapping)):
                raise ExecutionFailure('source_reuse_proof_tampered')
            other_outputs[batch['batch_id']] = translated
    if p.merge_evidence(other, other_outputs) != receipt['findings']:
        raise ExecutionFailure('source_reuse_findings_changed')
    journal['source_reuse'] = {'plan_digest': digest, 'source_receipt_sha256': sha256_digest(receipt),
                             'source_read_identity': plan['source_read_identity'],
                             'independent_reading': False}
    return receipt['findings']


def _persist_return(directory, value, phase, plan, runner):
    """Persist raw bytes and attribution before the returned state transition."""
    if isinstance(value, bytes):
        raw_bytes = value
    elif isinstance(value, str):
        raw_bytes = value.encode('utf-8')
    else:
        # The received CLI leaves its exact response file. SYNTH dict returns
        # are marked as adapter-serialized, not claimed as provider raw bytes.
        response = _safe(directory / 'response.json')
        if isinstance(runner, ProtocolCLI) and response.is_file():
            raw_bytes = _read_bytes(response, plan['limits']['max_output_bytes'])
            if json.loads(raw_bytes) != value:
                raise ExecutionFailure('runner_raw_response_mismatch')
        else:
            raw_bytes = canonical_bytes(value)
    limit = plan['limits']['max_output_bytes']
    if len(raw_bytes) > limit:
        raise ExecutionFailure('response_capture_limit')
    raw_path = _safe(directory / 'raw-return.json')
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open('xb') as stream:
        stream.write(raw_bytes); stream.flush(); os.fsync(stream.fileno())
    receipt = {'executor': EXECUTOR, 'plan_digest': plan['plan_digest'],
        'phase_id': phase['phase_id'], 'input_sha256': phase['input_sha256'],
        'raw_sha256': hashlib.sha256(raw_bytes).hexdigest(), 'raw_bytes': len(raw_bytes),
        'returned_at': timezone.now().isoformat(),
        'runner_identity': deepcopy(getattr(runner, 'identity', {})),
        'raw_origin': 'provider_response_file' if isinstance(runner, ProtocolCLI) else 'SYNTH_adapter',
        'native_network_retries': 'not_equated_to_application_phases'}
    for name in ('logical_input_sha256', 'logical_packet_sha256', 'transport_mode',
                 'transport_receipt_sha256', 'runner_identity_sha256', 'return_origin'):
        if name in phase:
            receipt[name] = phase[name]
    _write(directory / 'return-receipt.json', receipt)
    return receipt


def _translate(plan, batch, state, directory):
    _validate_return_transport(plan, batch['stage'], directory, state, _read(directory / 'return-receipt.json'))
    raw = _read(directory / 'raw-return.json')
    if batch['stage'] == 'evidence':
        translated, mapping = p.translate_evidence(plan, batch, raw)
    else:
        translated, mapping = p.translate_claims(plan, batch, raw)
    _write(directory / 'translated.json', translated)
    _write(directory / 'mapping.json', mapping)
    state.update(state='accepted', translated_sha256=sha256_digest(translated),
                 mapping_sha256=sha256_digest(mapping))
    return translated


def _checkpoint(plan, journal, attempt, user, *, accepted, next_batch, spent,
                close_lease, reason='pilot_checkpoint_received'):
    value = {'executor': EXECUTOR, 'plan_digest': plan['plan_digest'], 'owner_id': user.pk,
             'attempt_id': attempt.pk, 'accepted_batches_in_operation': accepted,
             'next_batch': next_batch, 'allocation_phases_spent': spent,
             'status': 'pilot_checkpoint_received', 'reason': reason, 'receipt_created': False,
             'publication': 'not_authorized', 'lease_closed': False,
             'expires_at_unchanged': attempt.expires_at.isoformat()}
    accepted_ids = [b['batch_id'] for b in plan['batches']
                    if journal['batches'].get(b['batch_id'], {}).get('state') == 'accepted']
    value['last_accepted_batch'] = accepted_ids[-1] if accepted_ids else None
    path = _state_directory(plan) / 'pilot-checkpoint.json'
    _write(path, value)
    if close_lease:
        with _application_write():
            v.interrupt(attempt.pk, user=user, error_code='interrupted')
        value.update(lease_closed=True, closure='explicit_owned_interrupt',
                     closure_at=timezone.now().isoformat())
        _write(path, value)
    history = _state_directory(plan) / 'checkpoints' / (
        f"attempt-{attempt.pk}-{timezone.now().strftime('%Y%m%dT%H%M%S%f')}.json")
    _write(history, value)
    raise ExecutionFailure('pilot_checkpoint_received')


def execute_plan(plan_path, *, user, allocation_path=None, execution_id,
                 runner=None, resume=True, retry_batches=(), stop_file=None, timeout=600,
                 codex_executable=None, stop_after_accepted_batches=None,
                 transport=None, close_lease_at_checkpoint=False,
                 pause_before_launch_file=None):
    """Launch only explicitly allocated phases; recover returned outputs for free.

    A failed batch requires its explicit retry name and another available phase.
    An ambiguous launched effect is never replayed, including with that option.
    """
    if not TOKEN.fullmatch(execution_id or ''):
        raise ValidationError('invalid_plan_execution_id')
    mode = transport or getattr(runner, 'transport_mode', 'legacy')
    if mode not in TRANSPORTS or type(close_lease_at_checkpoint) is not bool:
        raise ExecutionFailure('invalid_transport_or_checkpoint_option')
    if (stop_after_accepted_batches is not None and
            (type(stop_after_accepted_batches) is not int or stop_after_accepted_batches < 1)):
        raise ValidationError('invalid_pilot_batch_limit')
    plan = p.validate_plan(_read(plan_path, external=True))
    job = _current(plan, user)
    from .proof_architecture import require_plan_preflight
    require_plan_preflight(job.revision, plan)
    if 'proof_architecture' in plan and not plan['batches']:
        raise ExecutionFailure('no_semantic_batches_local_assessment_only')
    allocation = _allocation(allocation_path, plan, user)  # before any CLI constructor
    # One OS plan lock survives the operation. Allocation and SQLite write locks
    # are short and never held during provider supervision or another guard.
    guard = lambda: _allocation(allocation_path, plan, user)
    with _lock(_state_directory(plan) / 'lock'):
        journal_path, journal = _journal(plan, allocation)
        if not resume and journal['attempt_ids']:
            raise ExecutionFailure('existing_plan_requires_resume')
        ledger = _recover_allocation(plan, journal, allocation, guard=guard)
        _write(journal_path, journal)
        pending = _pending(plan, journal, retry_batches)
        if len(ledger['phases']) + pending > allocation['max_phases']:
            raise ExecutionFailure('allocation_insufficient_for_remaining_plan')
        if pending and job.state in {'completed', 'blocked', 'cancelled', 'stale'}:
            raise ExecutionFailure('job_not_restartable')
        active = job.attempts.filter(finished_at__isnull=True, expires_at__gt=timezone.now()).first()
        if active and active.pk not in journal['attempt_ids']:
            raise ExecutionFailure('unattributed_running_attempt_preserved')
        reused = _reuse_sources(plan, journal, user)
        attempt = None
        def check():
            _current(plan, user)
            require_plan_preflight(job.revision, plan)
            _allocation(allocation_path, plan, user)
            if attempt is not None:
                current_attempt = VerificationAttempt.objects.get(pk=attempt.pk)
                if current_attempt.actor_id != user.pk or current_attempt.job_id != plan['job_id']:
                    raise PermissionDenied('verification_attempt_owner')
                if current_attempt.stage != 'completed' and (
                        current_attempt.finished_at or current_attempt.expires_at <= timezone.now()):
                    raise ValidationError('attempt_closed_or_expired')
            if stop_file and Path(stop_file).exists():
                raise ExecutionFailure('interrupted')
        check()
        synthetic = runner is not None and getattr(runner, 'SYNTH', False) is True
        if runner is not None and not synthetic and not isinstance(runner, ProtocolCLI):
            raise ExecutionFailure('unqualified_plan_runner')
        if synthetic:
            if (settings.DV_ENV != 'test' or job.revision.dossier.data_scope != 'synthetic'
                    or not user.dv_profile.is_test_identity):
                raise ExecutionFailure('synthetic_runner_outside_synth_test_scope')
        if runner is None and pending:
            runner = ProtocolCLI(timeout=timeout, executable=codex_executable, transport=mode)
        if runner is not None:
            identity = getattr(runner, 'identity', {})
            if identity.get('model') != plan['model'] or identity.get('model_reasoning_effort') != plan['effort']:
                raise ExecutionFailure('runner_configuration_changed')
            if not synthetic and getattr(runner, 'transport_mode', 'legacy') != mode:
                raise ExecutionFailure('runner_transport_changed')
        if journal['attempt_ids']:
            attempt = VerificationAttempt.objects.select_related('job').get(pk=journal['attempt_ids'][-1])
            if attempt.actor_id != user.pk or attempt.job_id != job.pk:
                raise PermissionDenied('verification_attempt_owner')
        if attempt is None or (attempt.finished_at and attempt.stage != 'completed') or (
                attempt.stage != 'completed' and attempt.expires_at <= timezone.now()):
            with _application_write():
                attempt = v.start(job.pk, user=user, execution_id=execution_id,
                    provider='synthetic' if synthetic else 'codex_session', model=plan['model'], effort=plan['effort'])
            journal['attempt_ids'].append(attempt.pk)
            _write(journal_path, journal)
        if attempt.execution_id != execution_id:
            raise PermissionDenied('verification_execution_id_changed')
        if attempt.stage == 'completed':
            saved = _read(_state_directory(plan) / 'aggregate.json')
            if saved.get('execution') != 'complete' or not saved.get('result'):
                raise ExecutionFailure('completed_attempt_aggregate_missing')
            check()
            with _application_write():
                receipt = v.finish(attempt.pk, user=user, result=saved['result'])
            journal['result'] = {'execution': 'complete', 'documentary': receipt.decision,
                'diffusion': 'not_authorized', 'result_sha256': receipt.result_digest}
            journal['receipt_id'] = receipt.pk
            _write(journal_path, journal)
            return receipt
        source_outputs = {}
        claim_states = {}
        findings = reused
        accepted_in_operation = 0
        translation_started = False
        try:
            for batch_index, batch in enumerate(plan['batches']):
                translation_started = False
                if batch['stage'] == 'evidence' and reused is not None:
                    continue
                if batch['stage'] == 'claims' and findings is None:
                    findings = p.merge_evidence(plan, source_outputs)
                    with _application_write():
                        v.record_evidence(attempt.pk, user=user, findings=findings)
                    _store_source_receipt(plan, findings, owner_id=user.pk, attempt_id=attempt.pk)
                elif batch['stage'] == 'claims' and reused is not None and attempt.stage == 'evidence':
                    with _application_write():
                        v.record_evidence(attempt.pk, user=user, findings=findings)
                    attempt.refresh_from_db()
                check()
                state = journal['batches'].get(batch['batch_id'])
                if state and state['state'] in {'accepted', 'returned'}:
                    directory = _state_directory(plan) / batch['batch_id'] / str(state['run_number'])
                    previously_returned = state['state'] == 'returned'
                    translation_started = True
                    translated = _translate(plan, batch, state, directory)
                    if previously_returned:
                        _ack_phase(allocation, state['phase_id'],
                            {'state':'accepted','translated_sha256':state['translated_sha256'],
                             'mapping_sha256':state['mapping_sha256']})
                else:
                    packet = p.packet_for(plan, batch)
                    if batch['stage'] == 'claims':
                        packet['evidence_findings'] = findings
                    prompt_bytes, binding = _prompt_binding(batch['stage'], packet, mode)
                    binding.update(runner_identity_sha256=sha256_digest(runner.identity),
                        return_origin='SYNTH_adapter' if synthetic else 'provider_response_file')
                    if not synthetic and runner.prompt_for(batch['stage'], packet).encode('utf-8') != prompt_bytes:
                        raise ExecutionFailure('runner_effective_prompt_changed')
                    if len(prompt_bytes) > plan['limits']['max_input_bytes']:
                        raise ExecutionFailure('effective_input_limit_with_findings')
                    if pause_before_launch_file and Path(pause_before_launch_file).exists():
                        spent = len(_allocation_snapshot(allocation, guard=guard)['phases'])
                        _checkpoint(plan, journal, attempt, user, accepted=accepted_in_operation,
                            next_batch=batch['batch_id'], spent=spent, close_lease=close_lease_at_checkpoint,
                            reason='pause_before_launch')
                    if (attempt.expires_at - timezone.now()).total_seconds() < timeout + 30:
                        spent = len(_allocation_snapshot(allocation, guard=guard)['phases'])
                        _checkpoint(plan, journal, attempt, user, accepted=accepted_in_operation,
                            next_batch=batch['batch_id'], spent=spent, close_lease=close_lease_at_checkpoint,
                            reason='lease_budget_before_launch')
                    run_number = state['run_number'] + 1 if state else 1
                    phase, _ = _reserve_phase(allocation, plan, batch, attempt.pk, binding, run_number,
                        retry_of=state['phase_id'] if state else None, guard=guard)
                    journal['batches'][batch['batch_id']] = state = deepcopy(phase)
                    _write(journal_path, journal)
                    directory = _safe(_state_directory(plan) / batch['batch_id'] / str(run_number))
                    _write(directory / 'packet.json', packet)
                    _write(directory / 'schema.json', p.response_schema(batch['stage'], packet))
                    if mode == 'lossless-v1':
                        from . import verification_transport as wire
                        try:
                            wire.write_receipt(batch['stage'], packet, directory)
                        except wire.TransportFailure as error:
                            raise ExecutionFailure(error.code) from None
                    state.update(state='launched', launched_at=timezone.now().isoformat())
                    _ack_phase(allocation, phase['phase_id'],
                        {'state':'launched','launched_at':state['launched_at']}, guard=guard)
                    _write(journal_path, journal)
                    returned = runner.run(batch['stage'], packet, directory, check)
                    receipt = _persist_return(directory, returned, phase, plan, runner)
                    state.update(state='returned', raw_sha256=receipt['raw_sha256'])
                    _ack_phase(allocation, phase['phase_id'],
                        {'state':'returned','raw_sha256':receipt['raw_sha256']})
                    _write(journal_path, journal)
                    check()  # a late result cannot bypass revocation/freshness
                    translation_started = True
                    translated = _translate(plan, batch, state, directory)
                    _ack_phase(allocation, phase['phase_id'],
                        {'state':'accepted','translated_sha256':state['translated_sha256'],
                         'mapping_sha256':state['mapping_sha256']})
                _write(journal_path, journal)
                if batch['stage'] == 'evidence':
                    source_outputs[batch['batch_id']] = translated
                else:
                    claim_states[batch['batch_id']] = {'state': 'accepted', 'result': translated}
                accepted_in_operation += 1
                remaining = [future for future in plan['batches'][batch_index + 1:]
                             if not (future['stage'] == 'evidence' and reused is not None)]
                if (stop_after_accepted_batches is not None
                        and accepted_in_operation >= stop_after_accepted_batches and remaining):
                    if findings is None and all(b['batch_id'] in source_outputs
                            for b in plan['batches'] if b['stage'] == 'evidence'):
                        findings = p.merge_evidence(plan, source_outputs)
                        with _application_write():
                            v.record_evidence(attempt.pk, user=user, findings=findings)
                        _store_source_receipt(plan, findings, owner_id=user.pk, attempt_id=attempt.pk)
                    spent = len(_allocation_snapshot(allocation, guard=guard)['phases'])
                    _checkpoint(plan, journal, attempt, user, accepted=accepted_in_operation,
                        next_batch=remaining[0]['batch_id'], spent=spent, close_lease=close_lease_at_checkpoint)
            if findings is None:
                findings = p.merge_evidence(plan, source_outputs)
                with _application_write():
                    v.record_evidence(attempt.pk, user=user, findings=findings)
            aggregate = p.aggregate(plan, claim_states)
            if aggregate['execution'] != 'complete':
                raise ExecutionFailure('aggregate_incomplete')
            check()
            _write(_state_directory(plan) / 'aggregate.json', aggregate)
            with _application_write():
                receipt = v.finish(attempt.pk, user=user, result=aggregate['result'])
            journal['result'] = {'execution': 'complete', 'documentary': receipt.decision,
                'diffusion': 'not_authorized', 'result_sha256': receipt.result_digest}
            journal['receipt_id'] = receipt.pk
            _write(journal_path, journal)
            return receipt
        except (ExecutionFailure, ValidationError, PermissionDenied, KeyboardInterrupt) as error:
            code = error.code if isinstance(error, ExecutionFailure) else (
                error.messages[0] if isinstance(error, ValidationError) else 'authority_or_interrupt_changed')
            # Closed error codes only; no provider message, URLs, credentials or reasoning logs.
            if not re.fullmatch(r'[A-Za-z0-9_]{1,100}', str(code)):
                code = 'protocol_refused'
            if code == 'pilot_checkpoint_received':
                # Deliberate boundary after validated/persisted acceptance, not
                # an incomplete provider response. The explicitly requested
                # closure, if any, was recorded before reaching this boundary.
                raise
            if 'batch' in locals() and batch['batch_id'] in journal['batches']:
                state = journal['batches'][batch['batch_id']]
                if state['state'] == 'returned' and not translation_started:
                    # The effect and its exact return are already attributed.
                    # A subsequent stop/revocation/expired lease must not cause
                    # generation twice. Recheck authority before translation on
                    # resume; a translation failure still refuses below.
                    state['last_control_error'] = code
                    _ack_phase(allocation, state['phase_id'], {'last_control_error':code})
                elif state['state'] != 'accepted':
                    state['state'] = ('partial' if code == 'incomplete_claim_coverage' else
                        'interrupted' if code in {'timeout', 'interrupted', 'authority_or_interrupt_changed'} else 'refused')
                    state['error_code'] = code
                    _ack_phase(allocation, state['phase_id'], {'state':state['state'],'error_code':code})
            _write(journal_path, journal)
            _write(_state_directory(plan) / 'failure.json', {'executor': EXECUTOR,
                'code': code, 'automatic_retry': False, 'publication': 'not_requested'})
            try:
                if attempt.stage != 'completed':
                    with _application_write():
                        v.interrupt(attempt.pk, user=user, error_code=code if code in
                            {'interrupted', 'provider_unavailable', 'timeout', 'incomplete_response'} else 'incomplete_response')
            except (ValidationError, PermissionDenied):
                pass
            raise
