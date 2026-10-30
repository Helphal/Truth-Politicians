"""Projection explicite vers le Site lu, vérification Ed25519 sans clé privée.

Pas de conversion silencieuse ni d'envoi : le bridge draft.1 perd encore des
champs de preuve et ne fournit pas de reçu lié. Handoff candidat uniquement.
"""
import base64
import binascii
from copy import deepcopy
from django.core.exceptions import ValidationError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .canonical import canonical_bytes, sha256_digest

SITE_VERSION = "dv-site-bridge/0.1.0-draft.1"


def site_record_digest(record):
    # PHP DV_Security::record_digest retire review et sequence ; PHP échappe
    # U+2028/U+2029 même avec JSON_UNESCAPED_UNICODE (sans UNESCAPED_LINE_TERMINATORS).
    unsigned = {k:v for k,v in record.items() if k not in {"review", "sequence"}}
    encoded = canonical_bytes(unsigned).replace(b"\xe2\x80\xa8", b"\\u2028").replace(b"\xe2\x80\xa9", b"\\u2029")
    import hashlib
    return hashlib.sha256(encoded).hexdigest()


def verify_site_review(record, *, trusted_public_keys, environment, allow_test_fixtures=False):
    """Clés PUBLIQUES provenant de la configuration, jamais du document importé."""
    if environment not in {"test", "local", "private", "staging", "production"}:
        raise ValidationError("site_review_environment_unknown")
    proof = record.get("review")
    if not isinstance(proof, dict) or set(proof) != {"algorithm", "key_id", "snapshot_sha256", "signature"}:
        raise ValidationError("site_review_missing")
    if proof["algorithm"] != "ed25519":
        raise ValidationError("site_review_algorithm_unsupported")
    key_id = proof["key_id"]
    if not isinstance(key_id, str) or key_id not in trusted_public_keys:
        raise ValidationError("site_review_key_unknown")
    if record.get("is_test") or key_id.startswith("test-"):
        if environment == "production" or not allow_test_fixtures:
            raise ValidationError("site_test_review_rejected")
    digest = site_record_digest(record)
    if proof["snapshot_sha256"] != digest:
        raise ValidationError("site_review_snapshot_mismatch")
    try:
        key = base64.b64decode(trusted_public_keys[key_id], validate=True)
        signature = base64.b64decode(proof["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(signature, digest.encode("ascii"))
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise ValidationError("site_review_signature_invalid") from exc
    return digest


def prepare_comparison_handoff(event, *, detached_review=None, trusted_public_keys=None):
    """Le temps de revue vient de la revue enregistrée ; jamais de l'importeur.

    L'absence de signature produit un candidat NON autorisé, pas un booléen revue.
    Le paquet source exact reste présent pour examiner les pertes de projection.
    """
    from .publication import packet_for_event, evaluate_revision
    from atelier.models import HumanReview
    packet = packet_for_event(event)
    if event.operation != "upsert" or event.dossier.content_kind != "speech_vote":
        raise ValidationError("site_comparison_upsert_required")
    revision = event.snapshot.revision
    eligibility = evaluate_revision(revision, event.environment)
    if not eligibility.allowed or eligibility.review_id is None:
        raise ValidationError("site_current_human_review_required")
    public = packet["payload"]["public"]
    person = public.get("attribution", {}).get("person_id")
    if not person or not public.get("declaration_date"):
        raise ValidationError("site_person_or_exact_declaration_date_missing")
    review = HumanReview.objects.get(pk=eligibility.review_id)
    source_ids = ["DV:SRC:" + sha256_digest(s) for s in packet["payload"]["source_versions"]]
    scrutiny_ids = public.get("catalogue", {}).get("scope_scrutiny_ids", [])
    if not scrutiny_ids:
        raise ValidationError("site_scrutiny_scope_missing")
    from .publication import REQUIRED_CONTROLS
    record = {"record_type":"comparison", "record_id":packet["record_id"],
        "revision":packet["record_revision"], "sequence":packet["sequence"], "operation":"upsert",
        "publication_circuit":"analysis", "is_test":packet["payload"]["data_scope"] != "real",
        "controls":{k:True for k in sorted(REQUIRED_CONTROLS["speech_vote"])},
        "data":{"slug":"dv-" + sha256_digest(packet["record_id"])[:24], "title":public["title"],
            "declaration_quote":public["declaration_quote"], "declaration_date":public["declaration_date"],
            "author_person_external_id":person, "short_answer":public["short_answer"],
            "main_caveat":public["main_caveat"], "reasoning":public["reasoning"],
            "result_code":public["result"], "method_version":packet["payload"]["method_version"],
            "source_external_ids":source_ids, "scrutin_external_ids":scrutiny_ids,
            "reviewed_at":review.created_at.isoformat(), "public_status":"draft"}}
    verified = False
    if detached_review is not None:
        record["review"] = deepcopy(detached_review)
        verify_site_review(record, trusted_public_keys=trusted_public_keys or {},
            environment=event.environment, allow_test_fixtures=event.environment == "test")
        verified = True
    return {"format":"dv.atelier-site-handoff/0.1.0-draft.1", "target_bridge":SITE_VERSION,
        "source_packet":packet, "site_record_candidate":record,
        "site_record_sha256":site_record_digest(record), "external_ed25519_verified":verified,
        "send_authorized":False, "publication_authorized":False,
        "blocking_gaps":["Site draft.1 sans reçu signé lié et sans assemblage de catalogue paginé.",
            "Preuves/contre-éléments/attribution complète non représentés dans le modèle Site : adoption versionnée requise.",
            "La signature du record projeté ne couvre pas à elle seule les champs source absents du Site."]}
