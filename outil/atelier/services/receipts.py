"""Reçu de transport lié à un événement ; jamais preuve de publication publique."""
from .canonical import sha256_digest
from .publication import sign_packet, verify_packet_signature
from django.conf import settings


def receipt_for_packet(packet, status):
    if status not in {"applied", "duplicate"}:
        raise ValueError("receipt_status_invalid")
    receipt = {
        "receipt_schema_version": "dv.receipt/0.1.0-draft.1",
        "event_id": packet["event_id"],
        "sequence": packet["sequence"],
        "record_id": packet["record_id"],
        "environment": packet["environment"],
        "destination": packet["destination"],
        "key_id": settings.DV_RECEIPT_KEY_ID,
        "event_digest": sha256_digest(packet),
        "status": status,
    }
    return {**receipt, "signature_algorithm": "HMAC-SHA256", "signature": sign_packet(receipt)}


def receipt_verification_view(receipt: dict) -> dict:
    """Corps signé tel que renvoyé par le Site (sans métadonnées transport Python)."""
    return {
        k: v
        for k, v in receipt.items()
        if not str(k).startswith("_transport_") and not str(k).startswith("_loopback_")
    }


def verify_receipt(receipt, packet) -> bool:
    if not isinstance(receipt, dict):
        return False
    body = receipt_verification_view(receipt)
    if body.get("status") not in {"applied", "duplicate"}:
        return False
    expected = receipt_for_packet(packet, body["status"])
    if set(body) != set(expected):
        return False
    unsigned = {k: v for k, v in body.items() if k not in {"signature", "signature_algorithm"}}
    if not all(body[k] == expected[k] for k in expected if k != "signature"):
        return False
    signature = body.get("signature", "")
    if not isinstance(signature, str) or not signature:
        return False
    return verify_packet_signature(unsigned, signature)


def transport_duplicate(receipt: dict) -> bool:
    return bool(receipt.get("_transport_duplicate"))
