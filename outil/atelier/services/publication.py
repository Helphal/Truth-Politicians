"""Contrôles, revue humaine et autorisation de publication issus de l'état serveur."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from datetime import timedelta
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from atelier.services.review_authority import configured_final_review_authority_uid

from atelier.models import (
    AuditEvent,
    ControlRecord,
    DocumentVersion,
    EditorialDossier,
    EditorialRevision,
    Environment,
    HumanReview,
    OutboxEvent,
    PublicationSnapshot,
    RemoteExposure,
    UserProfile,
)

from .canonical import canonical_bytes, sha256_digest
from .contracts import validate_contract_envelope, validate_semantic_envelope
from .factual_checks import counts_check, calculation_check


COMMON_CONTROLS = {
    "source_fidelity",
    "identity",
    "procedure",
    "coverage",
    "public_sources",
    "reuse",
    "privacy",
}
REQUIRED_CONTROLS = {
    EditorialDossier.Kind.OFFICIAL_VOTE: COMMON_CONTROLS | {"counts"},
    EditorialDossier.Kind.VOTE_COMPARISON: COMMON_CONTROLS | {"calculation"},
    EditorialDossier.Kind.SPEECH_VOTE: COMMON_CONTROLS | {"comparability", "citations", "counterevidence"},
}
ALLOWED_PUBLIC_KEYS = {
    EditorialDossier.Kind.OFFICIAL_VOTE: {
        "institution", "legislature", "session", "official_title", "scrutin_id", "occurred_on",
        "date_precision", "procedure", "stage", "result_status",
        "official_text_url", "official_vote_url", "text_reference_role",
        "nominative_status", "counts", "coverage", "themes", "catalogue",
    },
    EditorialDossier.Kind.VOTE_COMPARISON: {
        "title", "scope", "rule_id", "observations", "calculation", "official_text_url",
        "official_vote_url", "coverage", "themes", "catalogue",
    },
    EditorialDossier.Kind.SPEECH_VOTE: {
        "public_contract_version", "diffusion_selection",
        "title", "short_answer", "main_caveat", "reasoning", "result",
        "declaration_quote", "declaration_date", "declaration_date_state", "declaration_source_url",
        "official_text_url", "official_vote_url", "scope", "counterevidence",
        "coverage", "method_explanation", "themes", "catalogue", "acts", "evidence", "attribution",
        "actors", "declaration_date_precision", "compared_actor_scope", "collective_distribution",
        "qualified_declaration", "qualification_material",
        "object_observations",
        "scrutiny_text_binding_refs",
        "personal_vote_selection",
    },
}
SENSITIVE_KEY = re.compile(
    r"(^|_)(reviewer|password|secret|token|cookie|private|local_path|original_path|ip_address|email|contact)(_|$)",
    re.IGNORECASE,
)


def _review_authority_binding_valid(review: HumanReview, profile: UserProfile, environment: str) -> bool:
    if environment == Environment.PRODUCTION and profile.is_test_identity:
        return False
    if not profile.is_account_usable():
        return False
    authority_uid = configured_final_review_authority_uid()
    if not authority_uid:
        return False
    if review.authority_internal_uid != authority_uid:
        return False
    return str(profile.internal_uid) == authority_uid


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str
    route: str | None = None
    review_id: int | None = None


def _validate_public_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost", ".test", ".invalid")):
            raise ValueError
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValidationError("Référence publique non admise") from exc


def _scan_public(value, path="$", urls=None):
    urls = [] if urls is None else urls
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE_KEY.search(key):
                raise ValidationError(f"Champ privé interdit dans l'export : {path}.{key}")
            if key.lower().endswith("_url"):
                if not isinstance(item, str):
                    raise ValidationError(f"Référence publique non admise : {path}.{key}")
                _validate_public_url(item)
                urls.append(item)
                continue
            _scan_public(item, f"{path}.{key}", urls)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_public(item, f"{path}[{index}]", urls)
    elif isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        _validate_public_url(value)
        urls.append(value)
    return urls


def validate_source_snapshot(snapshot: list) -> None:
    if not isinstance(snapshot, list) or not snapshot:
        raise ValidationError("Versions de sources absentes")
    for item in snapshot:
        if not isinstance(item, dict) or set(item) != {"source_id", "version_id", "sha256", "public_url"}:
            raise ValidationError("Version de source invalide")
        if not isinstance(item["source_id"], str) or not item["source_id"]:
            raise ValidationError("Identifiant de source invalide")
        if not isinstance(item["version_id"], str) or not item["version_id"]:
            raise ValidationError("Identifiant de version invalide")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValidationError("Empreinte de source invalide")
        if not isinstance(item["public_url"], str):
            raise ValidationError("Référence publique non admise")
        _validate_public_url(item["public_url"])


def validate_public_payload(kind: str, payload: dict) -> None:
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("Charge publique vide ou invalide")
    allowed = ALLOWED_PUBLIC_KEYS.get(kind)
    if allowed is None or not set(payload).issubset(allowed):
        raise ValidationError("Champ public inattendu pour cette famille")
    _scan_public(payload)
    required_links = {"official_text_url", "official_vote_url"}
    if not required_links.issubset(payload) or payload["official_text_url"] == payload["official_vote_url"]:
        raise ValidationError("Deux références officielles distinctes sont obligatoires")
    _validate_public_url(payload["official_text_url"])
    _validate_public_url(payload["official_vote_url"])


def source_snapshot_digest(revision: EditorialRevision) -> str:
    return sha256_digest(revision.source_snapshot)


def _sources_current(revision: EditorialRevision) -> bool:
    if not isinstance(revision.source_snapshot, list) or not revision.source_snapshot:
        return False
    for source in revision.source_snapshot:
        if set(source) != {"source_id", "version_id", "sha256", "public_url"}:
            return False
        document = DocumentVersion.objects.filter(
            source__stable_id=source["source_id"],
            version_id=source["version_id"],
            sha256=source["sha256"],
            quarantined=False,
        ).first()
        latest = DocumentVersion.objects.filter(
            source__stable_id=source["source_id"], quarantined=False
        ).order_by("-captured_at", "-pk").first()
        if document is None or latest is None or latest.pk != document.pk:
            return False
        if source["public_url"] != document.source.document_url:
            return False
        try:
            _validate_public_url(source["public_url"])
        except ValidationError:
            return False
    return True


def evaluate_revision(revision: EditorialRevision, environment: str) -> Eligibility:
    revision = EditorialRevision.objects.select_related('dossier').get(pk=revision.pk)
    # Relire l'état d'autorité depuis la base. Une instance de révision peut
    # conserver une relation ``dossier`` en cache alors qu'une nouvelle
    # révision a été créée dans une autre transaction/instance.
    dossier = EditorialDossier.objects.get(pk=revision.dossier_id)
    if environment not in Environment.values:
        return Eligibility(False, "unknown_environment")
    if dossier.content_kind != dossier.established_kind:
        return Eligibility(False, "kind_changed")
    if dossier.suspended or dossier.state == EditorialDossier.State.SUSPENDED:
        return Eligibility(False, "suspended")
    if dossier.current_revision != revision.revision:
        return Eligibility(False, "not_current_revision")
    if environment == Environment.PRODUCTION and dossier.data_scope != "real":
        return Eligibility(False, "synthetic_content_in_production")
    if sha256_digest(revision.public_payload) != revision.snapshot_digest:
        return Eligibility(False, "snapshot_digest_mismatch")
    try:
        validate_public_payload(dossier.content_kind, revision.public_payload)
        if (dossier.data_scope == "real" and revision.verification_required
                and dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE):
            from .contracts import validate_speech_vote_payload
            validate_speech_vote_payload(revision.public_payload)
    except ValidationError:
        return Eligibility(False, "invalid_public_payload")
    from .verification import gate
    verification_ok, verification_reason = gate(revision)
    if not verification_ok:
        return Eligibility(False, verification_reason)
    architecture = None
    if dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE:
        from .proof_architecture import assessment_for_revision, documentary_gate, architecture_required
        architecture = assessment_for_revision(revision)
        if architecture_required(revision):
            proof_ok, proof_reason = documentary_gate(revision)
            if not proof_ok:
                return Eligibility(False, proof_reason)
    if dossier.content_kind in {EditorialDossier.Kind.OFFICIAL_VOTE, EditorialDossier.Kind.VOTE_COMPARISON}:
        check = counts_check if dossier.content_kind == EditorialDossier.Kind.OFFICIAL_VOTE else calculation_check
        passed, reason = check(revision.public_payload)
        if not passed:
            return Eligibility(False, reason)
    if not _sources_current(revision):
        return Eligibility(False, "source_changed_or_unknown")
    from .catalogue import binding_check
    bound, binding_reason = binding_check(revision)
    if not bound:
        return Eligibility(False, binding_reason)

    controls = list(revision.controls.filter(environment=environment).order_by("control_type", "-created_at"))
    latest = {}
    for control in controls:
        latest.setdefault(control.control_type, control)
    required = REQUIRED_CONTROLS[dossier.content_kind]
    if not required.issubset(latest):
        return Eligibility(False, "controls_missing")
    if any(not latest[name].passed or latest[name].input_digest != revision.snapshot_digest for name in required):
        return Eligibility(False, "controls_failed_or_stale")
    # Les qualifications de droits et les objets liés peuvent changer sans changer
    # le JSON public : un ancien enregistrement de contrôle ne suffit pas.
    from .revisions import deterministic_control_results
    current_controls = deterministic_control_results(revision)
    if any(not current_controls[name][0] for name in required):
        return Eligibility(False, "current_controls_failed")

    if dossier.content_kind != EditorialDossier.Kind.SPEECH_VOTE:
        return Eligibility(True, "eligible_not_published", "automatic_checked")

    review = revision.human_reviews.filter(environment=environment).order_by("-created_at", "-pk").first()
    if not review:
        return Eligibility(False, "review_missing_or_stale")
    if architecture is not None and review.proof_architecture_digest != architecture['assessment_digest']:
        return Eligibility(False, 'human_review_proof_changed')
    try:
        profile = review.reviewer.dv_profile
    except UserProfile.DoesNotExist:
        return Eligibility(False, "reviewer_not_authorized")
    if (
        review.revoked
        or review.decision != HumanReview.Decision.APPROVE
        or review.reviewed_digest != revision.snapshot_digest
        or review.evidence_snapshot_digest != source_snapshot_digest(revision)
        or review.created_via not in {"human_ui", "detached_ui"}
        or not profile.active
        or not _review_authority_binding_valid(review, profile, environment)
        or not review.reviewer.is_active
        or not review.reviewer.has_usable_password()
        or (environment == Environment.PRODUCTION and profile.is_test_identity)
    ):
        return Eligibility(False, "review_missing_or_stale")
    if review.created_via == 'detached_ui':
        from .detached_review import verify_current_receipt
        if not verify_current_receipt(review):
            return Eligibility(False, 'detached_review_missing_or_stale')
    return Eligibility(True, "eligible_not_published", "human_reviewed", review.pk)


@transaction.atomic
def record_human_review(*, dossier_id: int, expected_revision: int, expected_digest: str, user, decision: str,
                        environment: str, note: str = "") -> HumanReview:
    dossier = EditorialDossier.objects.select_for_update().get(pk=dossier_id)
    if not user.is_authenticated or not user.is_active or not user.has_usable_password():
        raise PermissionDenied("Compte humain authentifié requis")
    try:
        profile = user.dv_profile
    except UserProfile.DoesNotExist as exc:
        raise PermissionDenied("Rôle de revue absent") from exc
    from .review_authority import configured_final_review_authority_uid, may_record_human_review_decision

    if not profile.active or not may_record_human_review_decision(profile, decision, environment):
        raise PermissionDenied("Le compte ne peut pas effectuer de revue")
    if environment == Environment.PRODUCTION and profile.is_test_identity:
        raise PermissionDenied("Une identité fictive ne peut pas approuver en production")
    if decision not in HumanReview.Decision.values:
        raise ValidationError("Décision de revue inconnue")
    if dossier.current_revision != expected_revision:
        raise ValidationError("revision_conflict")
    revision = dossier.revisions.get(revision=expected_revision)
    if revision.snapshot_digest != expected_digest:
        raise ValidationError("snapshot_conflict")
    from .proof_architecture import assessment_for_revision, architecture_required
    architecture = assessment_for_revision(revision)
    if decision == HumanReview.Decision.APPROVE:
        if architecture_required(revision):
            if architecture is None or architecture['gates']['eligible_for_human_review']['status'] != 'complete':
                raise ValidationError('human_review_documentary_proof_not_ready')

    authority_binding = ""
    if decision in {HumanReview.Decision.APPROVE, HumanReview.Decision.REJECT}:
        authority_binding = configured_final_review_authority_uid()
    review = HumanReview.objects.create(
        revision=revision,
        reviewer=user,
        decision=decision,
        reviewed_digest=expected_digest,
        evidence_snapshot_digest=source_snapshot_digest(revision),
        environment=environment,
        authority_internal_uid=authority_binding,
        proof_architecture_digest=architecture['assessment_digest'] if architecture else '',
        note=note,
    )
    next_state = {
        HumanReview.Decision.APPROVE: EditorialDossier.State.APPROVED_REVISION,
        HumanReview.Decision.REQUEST_CHANGES: EditorialDossier.State.DRAFT,
        HumanReview.Decision.REQUEST_EVIDENCE: EditorialDossier.State.NEEDS_EVIDENCE,
        HumanReview.Decision.REJECT: EditorialDossier.State.WITHDRAWN,
    }[decision]
    dossier.state = next_state
    dossier.save(update_fields=["state", "updated_at"])
    if decision != HumanReview.Decision.APPROVE:
        for event in OutboxEvent.objects.filter(dossier=dossier, environment=environment, operation=OutboxEvent.Operation.UPSERT):
            invalidate_event(event, "human_review_withdrawn")
    AuditEvent.objects.create(
        actor=user,
        event_type="human_review_recorded",
        object_type="editorial_revision",
        object_id=f"{dossier.stable_id}:{revision.revision}",
        detail={"decision": decision, "environment": environment, "digest": expected_digest},
    )
    return review


def _attestation_key() -> bytes:
    key = settings.DV_REVIEW_ATTESTATION_KEY or (settings.SECRET_KEY if settings.DV_ENV in {"local", "test"} else "")
    if not key:
        raise ValidationError("Clé d'attestation absente")
    return key.encode("utf-8")


def _event_key(packet: dict) -> bytes:
    environment = packet.get("environment")
    destination = packet.get("destination")
    key_id = packet.get("key_id")
    if destination != f"dv-site:{environment}":
        raise ValidationError("event_destination_mismatch")
    if settings.DV_ENV in {"local", "test"} and environment == "test" and key_id == "test-v1":
        return _attestation_key()
    entry = settings.DV_EVENT_KEYS.get(key_id, {})
    purpose = "receipt" if "receipt_schema_version" in packet else "event"
    if entry.get("environment") != environment or entry.get("destination") != destination or entry.get("purpose") != purpose or not entry.get("active"):
        raise ValidationError("event_key_not_authorized")
    secret = entry.get("secret", "")
    if isinstance(secret, (bytes, bytearray)):
        key_material = bytes(secret)
    elif isinstance(secret, str):
        key_material = secret.encode("utf-8")
    else:
        raise ValidationError("event_key_missing")
    if len(key_material) < 32:
        raise ValidationError("event_key_missing")
    return key_material


def sign_packet(packet_without_signature: dict) -> str:
    return hmac.new(_event_key(packet_without_signature), canonical_bytes(packet_without_signature), hashlib.sha256).hexdigest()


def verify_packet_signature(packet_without_signature: dict, signature: str) -> bool:
    try:
        return isinstance(signature, str) and hmac.compare_digest(sign_packet(packet_without_signature), signature)
    except ValidationError:
        return False


@transaction.atomic
def authorize_and_queue(revision: EditorialRevision, environment: str) -> OutboxEvent:
    dossier = EditorialDossier.objects.select_for_update().get(pk=revision.dossier_id)
    revision = EditorialRevision.objects.select_for_update().get(pk=revision.pk)
    # Dissemination revisions may only use the explicitly selected v3 circuit.
    # A configurable legacy envelope must never copy their private archive.
    from .diffusion import PUBLIC_CONTRACT
    if revision.public_payload.get('public_contract_version') == PUBLIC_CONTRACT:
        raise ValidationError('diffusion_requires_verification_site_v3')
    eligibility = evaluate_revision(revision, environment)
    if not eligibility.allowed:
        raise ValidationError(f"publication_refused:{eligibility.reason}")
    review_metadata = {"nature": "controles_automatiques"}
    if eligibility.review_id is not None:
        review = HumanReview.objects.get(pk=eligibility.review_id)
        # Date civile de la revue, toujours dans le fuseau du projet, pas celui du navigateur.
        from zoneinfo import ZoneInfo
        review_metadata = {"nature": "revue_humaine", "date": timezone.localtime(review.created_at, ZoneInfo(settings.TIME_ZONE)).date().isoformat()}
    snapshot, created = PublicationSnapshot.objects.get_or_create(
        revision=revision,
        environment=environment,
        defaults={
            "schema_version": settings.DV_PUBLICATION_SCHEMA,
            "policy_version": settings.DV_POLICY_VERSION,
            "public_payload": revision.public_payload,
            "snapshot_digest": revision.snapshot_digest,
            "authorization_route": eligibility.route,
            "public_review_metadata": review_metadata,
        },
    )
    if not created and snapshot.snapshot_digest != revision.snapshot_digest:
        raise ValidationError("snapshot_conflict")
    if not created and snapshot.public_review_metadata != review_metadata:
        raise ValidationError("review_changed_new_revision_required")
    existing = OutboxEvent.objects.filter(
        dossier=dossier, snapshot=snapshot, operation=OutboxEvent.Operation.UPSERT
    ).order_by("-sequence").first()
    if existing:
        if existing.state == OutboxEvent.State.FAILED:
            raise ValidationError("invalidated_event_new_revision_required")
        return existing
    sequence = (OutboxEvent.objects.filter(dossier=dossier).aggregate(v=Max("sequence"))["v"] or 0) + 1
    body = {
        "schema_version": settings.DV_PUBLICATION_SCHEMA,
        "policy_version": settings.DV_POLICY_VERSION,
        "record_id": dossier.stable_id,
        "record_revision": revision.revision,
        "content_kind": dossier.content_kind,
        "data_scope": dossier.data_scope,
        "method_version": revision.method_version or None,
        "source_versions": revision.source_snapshot,
        "public": revision.public_payload,
        "authorization": {
            "route": eligibility.route,
            "snapshot_digest": revision.snapshot_digest,
            "public_review": snapshot.public_review_metadata,
        },
    }
    if dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE:
        from .racc001_contract import attach_qualification_fingerprints

        body = attach_qualification_fingerprints(body)
    validate_contract_envelope(body)
    validate_semantic_envelope(body)
    event = OutboxEvent.objects.create(
        dossier=dossier,
        snapshot=snapshot,
        operation=OutboxEvent.Operation.UPSERT,
        environment=environment,
        content_revision=revision.revision,
        sequence=sequence,
        idempotency_key=f"{dossier.stable_id}:{sequence}:upsert:{revision.snapshot_digest}",
        payload=body,
        payload_digest=sha256_digest(body),
    )
    dossier.state = EditorialDossier.State.QUEUED
    dossier.save(update_fields=["state", "updated_at"])
    return event


@transaction.atomic
def queue_suspension(
    dossier: EditorialDossier,
    public_reason: str,
    environment: str = Environment.TEST,
) -> OutboxEvent:
    if environment not in Environment.values:
        raise ValidationError("unknown_environment")
    dossier = EditorialDossier.objects.select_for_update().get(pk=dossier.pk)
    dossier.suspended = True
    dossier.suspension_reason = public_reason
    dossier.state = EditorialDossier.State.SUSPENDED
    dossier.save(update_fields=["suspended", "suspension_reason", "state", "updated_at"])
    from .reconciliation import remember_exposure
    emitted = OutboxEvent.objects.filter(dossier=dossier, environment=environment,
        operation="upsert", issued_at__isnull=False)
    for previous in emitted:
        remember_exposure(previous)
    max_emitted = emitted.aggregate(v=Max("sequence"))["v"] or 0
    existing = OutboxEvent.objects.filter(dossier=dossier, environment=environment,
        operation="suspend", sequence__gt=max_emitted).order_by("-sequence").first()
    if existing:
        RemoteExposure.objects.filter(event__dossier=dossier, event__environment=environment,
            resolved_at__isnull=True).update(withdrawal_requested=True, suspension=existing)
        return existing
    sequence = (OutboxEvent.objects.filter(dossier=dossier).aggregate(v=Max("sequence"))["v"] or 0) + 1
    body = {
        "schema_version": settings.DV_PUBLICATION_SCHEMA,
        "record_id": dossier.stable_id,
        "record_revision": max(dossier.current_revision, 1),
        "operation": "suspend",
        "public_reason": public_reason,
    }
    _scan_public(body)
    event = OutboxEvent.objects.create(
        dossier=dossier,
        operation=OutboxEvent.Operation.SUSPEND,
        environment=environment,
        content_revision=max(dossier.current_revision, 1),
        sequence=sequence,
        idempotency_key=f"{dossier.stable_id}:{sequence}:suspend",
        payload=body,
        payload_digest=sha256_digest(body),
    )
    RemoteExposure.objects.filter(event__dossier=dossier, event__environment=environment,
        resolved_at__isnull=True).update(withdrawal_requested=True, suspension=event)
    return event


def validate_event_current(event: OutboxEvent, *, allow_delivered=False, check_expiry=True) -> None:
    """Un événement sérialisé ne remplace jamais l'autorité courante de l'atelier."""
    states = {OutboxEvent.State.PENDING, OutboxEvent.State.SENDING}
    if allow_delivered:
        states.add(OutboxEvent.State.DELIVERED)
    if event.state not in states:
        raise ValidationError("event_not_pending")
    if sha256_digest(event.payload) != event.payload_digest:
        raise ValidationError("event_payload_changed")
    if event.payload.get("record_id") != event.dossier.stable_id or event.payload.get("record_revision") != event.content_revision:
        raise ValidationError("event_identity_mismatch")
    if event.operation == OutboxEvent.Operation.UPSERT:
        if check_expiry and event.state != OutboxEvent.State.DELIVERED and event.issued_at and event.issued_at + timedelta(seconds=settings.DV_EVENT_TTL_SECONDS) <= timezone.now():
            raise ValidationError("event_expired_reissue_required")
        if event.payload.get("schema_version") != settings.DV_PUBLICATION_SCHEMA:
            raise ValidationError("legacy_event_requires_new_revision")
        if not event.snapshot_id:
            raise ValidationError("event_snapshot_missing")
        revision = EditorialRevision.objects.get(pk=event.snapshot.revision_id)
        decision = evaluate_revision(revision, event.environment)
        if not decision.allowed:
            raise ValidationError(decision.reason)
        exported_public = event.payload.get("public")
        if exported_public != revision.public_payload:
            raise ValidationError("event_snapshot_mismatch")
        if event.payload.get("authorization", {}).get("snapshot_digest") != revision.snapshot_digest:
            raise ValidationError("event_snapshot_mismatch")
        validate_contract_envelope(event.payload)
        if decision.review_id:
            from zoneinfo import ZoneInfo
            review = HumanReview.objects.get(pk=decision.review_id)
            date = timezone.localtime(review.created_at, ZoneInfo(settings.TIME_ZONE)).date().isoformat()
            if event.payload["authorization"]["public_review"] != {"nature": "revue_humaine", "date": date}:
                raise ValidationError("event_review_changed")
    elif event.operation != OutboxEvent.Operation.SUSPEND or event.payload.get("operation") != "suspend":
        raise ValidationError("event_operation_invalid")


@transaction.atomic
def reissue_expired_event(event):
    """Nouveau message, mêmes preuves : jamais prolonger ou resign­er l'ancien fichier."""
    event = OutboxEvent.objects.select_for_update().select_related("dossier", "snapshot").get(pk=event.pk)
    EditorialDossier.objects.select_for_update().get(pk=event.dossier_id)
    validate_event_current(event, check_expiry=False)
    if event.operation != OutboxEvent.Operation.UPSERT or not event.issued_at or event.issued_at + timedelta(seconds=settings.DV_EVENT_TTL_SECONDS) > timezone.now():
        raise ValidationError("event_not_expired")
    event.state = OutboxEvent.State.FAILED
    event.last_error_code = "superseded_expired_event"
    event.save(update_fields=["state", "last_error_code", "updated_at"])
    sequence = (OutboxEvent.objects.filter(dossier=event.dossier).aggregate(v=Max("sequence"))["v"] or 0) + 1
    return OutboxEvent.objects.create(dossier=event.dossier, snapshot=event.snapshot,
        operation=event.operation, environment=event.environment, content_revision=event.content_revision,
        sequence=sequence, idempotency_key=f"{event.dossier.stable_id}:{sequence}:reissue:{event.payload_digest}",
        payload=event.payload, payload_digest=event.payload_digest)


def invalidate_event(event, reason):
    event.state = OutboxEvent.State.FAILED
    event.last_error_code = reason[:100]
    event.save(update_fields=["state", "last_error_code", "updated_at"])
    # Une copie a pu partir : transmettre une suspension persistante, pas seulement effacer la file.
    if event.operation == OutboxEvent.Operation.UPSERT and event.issued_at:
        queue_suspension(event.dossier, "Contenu retiré en attente de vérification de ses preuves et autorisations.", event.environment)


@transaction.atomic
def packet_for_event(event: OutboxEvent, *, allow_delivered=False) -> dict:
    event = OutboxEvent.objects.select_for_update().select_related("dossier", "snapshot").get(pk=event.pk)
    validate_event_current(event, allow_delivered=allow_delivered)
    if event.issued_at is None:
        event.issued_at = timezone.now()
        event.signing_key_id = settings.DV_EVENT_KEY_ID
        event.save(update_fields=["issued_at", "signing_key_id", "updated_at"])
    if event.operation == OutboxEvent.Operation.UPSERT and event.issued_at + timedelta(seconds=settings.DV_EVENT_TTL_SECONDS) <= timezone.now():
        raise ValidationError("event_expired_reissue_required")
    from .reconciliation import remember_exposure
    remember_exposure(event)
    packet = {
        "event_schema_version": settings.DV_EVENT_SCHEMA,
        "environment": str(event.environment),
        "destination": f"dv-site:{event.environment}",
        "key_id": event.signing_key_id,
        "expires_at": (event.issued_at + timedelta(seconds=settings.DV_EVENT_TTL_SECONDS)).isoformat(),
        "event_id": str(event.event_id),
        "sequence": event.sequence,
        "operation": str(event.operation),
        "record_id": event.dossier.stable_id,
        "record_revision": event.content_revision,
        "payload_digest": event.payload_digest,
        "payload": event.payload,
    }
    return {**packet, "signature_algorithm": "HMAC-SHA256", "signature": sign_packet(packet)}
