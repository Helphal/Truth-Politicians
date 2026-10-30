"""Simulateur persistant du récepteur Site pour les tests de contrat locaux."""
from __future__ import annotations

import uuid
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from django.db import transaction

from atelier.models import ReceiverCursor, ReceiverEventLedger

from .canonical import sha256_digest
from .contracts import validate_event_packet_schema, validate_semantic_envelope
from .publication import verify_packet_signature
from django.core.exceptions import ValidationError


@transaction.atomic
def apply_packet(packet: dict, *, resume_authorized: bool = False) -> str:
    required = {
        "event_id", "sequence", "operation", "record_id", "record_revision", "payload_digest",
        "payload", "signature_algorithm", "signature",
        "event_schema_version", "environment", "destination", "key_id", "expires_at",
    }
    if not isinstance(packet, dict) or set(packet) != required or packet["signature_algorithm"] != "HMAC-SHA256":
        return "invalid_packet"
    if settings.DV_ENV not in {"local", "test"} or packet.get("environment") != "test" or packet.get("destination") != "dv-site:test":
        return "wrong_destination_environment"
    try:
        validate_event_packet_schema(packet)
    except ValidationError:
        return "contract_incompatible"
    unsigned = {key: packet[key] for key in required - {"signature_algorithm", "signature"}}
    if not verify_packet_signature(unsigned, packet["signature"]):
        return "unauthorized"
    try:
        event_id = uuid.UUID(packet["event_id"])
    except (ValueError, TypeError, AttributeError):
        return "invalid_packet"
    if type(packet["sequence"]) is not int or packet["sequence"] < 1:
        return "invalid_packet"
    if type(packet["record_revision"]) is not int or packet["record_revision"] < 1:
        return "invalid_packet"
    if packet["operation"] not in {"upsert", "suspend"}:
        return "unknown_operation"
    if sha256_digest(packet["payload"]) != packet["payload_digest"]:
        return "payload_digest_mismatch"
    if packet["payload"].get("schema_version") != settings.DV_PUBLICATION_SCHEMA:
        return "contract_incompatible"
    expires = parse_datetime(packet["expires_at"])
    if not expires or timezone.is_naive(expires) or (packet["operation"] == "upsert" and expires <= timezone.now()):
        return "event_expired"
    if packet["payload"].get("record_id") != packet["record_id"] or packet["payload"].get("record_revision") != packet["record_revision"]:
        return "packet_payload_mismatch"
    if packet["operation"] == "upsert":
        if sha256_digest(packet["payload"]["public"]) != packet["payload"]["authorization"]["snapshot_digest"]:
            return "snapshot_digest_mismatch"
        try:
            validate_semantic_envelope(packet["payload"])
        except ValidationError:
            return "public_control_failed"
        if (
            packet["payload"].get("record_id") != packet["record_id"]
            or packet["payload"].get("record_revision") != packet["record_revision"]
        ):
            return "packet_payload_mismatch"
    elif packet["payload"].get("operation") != "suspend":
        return "packet_payload_mismatch"

    cursor, _ = ReceiverCursor.objects.select_for_update().get_or_create(record_id=packet["record_id"])
    ledger = ReceiverEventLedger.objects.filter(event_id=event_id).first()
    if ledger and ledger.event_digest != sha256_digest(packet):
        return "event_identity_conflict"
    if packet["sequence"] == cursor.sequence:
        return "duplicate" if cursor.last_event_id == event_id and cursor.last_event_digest == sha256_digest(packet) else "sequence_conflict"
    if packet["sequence"] < cursor.sequence:
        return "stale_event"
    if packet["operation"] == "suspend":
        cursor.suspended = True
    else:
        if packet["record_revision"] < cursor.revision:
            return "stale_revision"
        if packet["record_revision"] == cursor.revision and cursor.snapshot_digest != packet["payload_digest"]:
            return "revision_conflict"
        if cursor.suspended and not resume_authorized:
            return "suspension_latched"
        cursor.suspended = False
        cursor.revision = packet["record_revision"]
        cursor.snapshot_digest = packet["payload_digest"]
        cursor.active_payload = packet["payload"]
    cursor.sequence = packet["sequence"]
    cursor.last_event_id = event_id
    cursor.last_event_digest = sha256_digest(packet)
    cursor.save()
    ReceiverEventLedger.objects.create(event_id=event_id, record_id=packet["record_id"], sequence=packet["sequence"], event_digest=sha256_digest(packet))
    return "applied_locally"
