"""Qualification exacte et conservatrice du scrutin Sénat 178."""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from atelier.models import AuditEvent, DocumentVersion, FactQualification, Scrutiny, TextVersion

from .canonical import sha256_digest
from .materialization import qualify
from .parsers.senate import parse_scrutiny_html


SCRUTINY_ID = "SENAT:2025-2026:SCRUTIN:178"
VOTE_URL = "https://www.senat.fr/scrutin-public/2025/scr2025-178.html"
ADOPTED_TEXT_URL = "https://www.senat.fr/leg/tas25-050.html"
SOURCE_IDS = (
    "SENAT_TEXT_PPL25_298",
    "SENAT_AMENDMENTS_ADOPTED_298",
    "SENAT_AMENDMENT_298_13",
    "SENAT_SESSION_20260129_013",
    "SENAT_TEXT_ADOPTED_050",
    "SENAT_SCRUTIN_2025_178",
    "SENAT_DOSSIER_PPL25_185",
)


def _plain(raw: bytes) -> str:
    value = raw.decode("utf-8", "strict")
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = value.replace("’", "'").replace(" ", " ")
    return " ".join(value.split()).lower()


def verify_chain_contents(contents: dict[str, bytes]) -> dict:
    if set(contents) != set(SOURCE_IDS):
        raise ValidationError("senate_chain_sources_incomplete")
    plain = {source_id: _plain(raw) for source_id, raw in contents.items()}
    base = plain["SENAT_TEXT_PPL25_298"]
    if "auprès d'une région d'outre-mer" not in base or "la production maximale d'hydrocarbures" not in base:
        raise ValidationError("senate_text_298_unexpected")

    amendments_raw = contents["SENAT_AMENDMENTS_ADOPTED_298"].decode("utf-8", "strict")
    adopted_numbers = set(re.findall(r"Amdt_(\d+)\.html", amendments_raw))
    if adopted_numbers != {"4", "9", "12", "13"}:
        raise ValidationError("senate_adopted_amendment_set_mismatch")
    amendment_13 = plain["SENAT_AMENDMENT_298_13"]
    if not all(
        token in amendment_13
        for token in (
            "adopté",
            "article 1er",
            "collectivité territoriale de saint-pierre-et-miquelon",
            "insérer les mots",
        )
    ):
        raise ValidationError("senate_amendment_13_unexpected")

    session = plain["SENAT_SESSION_20260129_013"]
    sequence = (
        session.find("l'article 2 est supprimé"),
        session.find("je mets aux voix, dans le texte de la commission, modifié"),
        session.find("résultat du scrutin n° 178"),
    )
    if min(sequence) < 0 or sequence != tuple(sorted(sequence)):
        raise ValidationError("senate_session_sequence_unexpected")

    adopted = plain["SENAT_TEXT_ADOPTED_050"]
    operative_passages = (
        "collectivité territoriale de saint-pierre-et-miquelon",
        "2° le ii de l'article 25 est abrogé.",
        "article 2 (supprimé)",
    )
    if any(passage not in adopted for passage in operative_passages):
        raise ValidationError("senate_adopted_text_050_unexpected")

    normalized = parse_scrutiny_html(contents["SENAT_SCRUTIN_2025_178"], VOTE_URL)
    expected = {
        "source_identifier": "178",
        "voters": 340,
        "expressed": 332,
        "count_for": 227,
        "count_against": 105,
        "count_abstention": 8,
    }
    if any(normalized.get(key) != value for key, value in expected.items()):
        raise ValidationError("senate_scrutiny_178_unexpected")
    return {
        "base_text": "298",
        "adopted_amendments": ["13", "4", "9", "12"],
        "sequence": ["amendment_13", "article_2_deleted", "whole_text_vote_178"],
        "resulting_text": "50",
        "verification_level": "documented_reconstruction",
        "operative_passages": [
            {
                "source_id": "SENAT_TEXT_ADOPTED_050",
                "locator": "article_1er:dispositif_2;article_2",
                "normalized_text": passage,
            }
            for passage in operative_passages
        ],
        "counts": {key: normalized[key] for key in expected if key != "source_identifier"},
    }


def _latest_documents() -> dict[str, DocumentVersion]:
    documents = {}
    runtime = settings.RUNTIME_DIR.resolve()
    for source_id in SOURCE_IDS:
        document = (
            DocumentVersion.objects.select_related("source")
            .filter(source__stable_id=source_id, quarantined=False)
            .order_by("-captured_at", "-pk")
            .first()
        )
        if not document:
            raise ValidationError(f"senate_document_missing:{source_id}")
        path = (runtime / document.original_path).resolve()
        if runtime not in path.parents or not path.is_file():
            raise ValidationError(f"senate_document_file_missing:{source_id}")
        raw = path.read_bytes()
        receipt = document.collection_receipt
        if (
            hashlib.sha256(raw).hexdigest() != document.sha256
            or len(raw) != document.byte_size
            or not isinstance(receipt, dict)
            or receipt.get("transport") != "protected_capture"
            or receipt.get("complete") is not True
            or receipt.get("content_sha256") != document.sha256
            or receipt.get("size_bytes") != document.byte_size
            or receipt.get("no_direct_fallback") is not True
        ):
            raise ValidationError(f"senate_document_provenance_invalid:{source_id}")
        documents[source_id] = document
    return documents


@transaction.atomic
def qualify_senate_178() -> tuple[Scrutiny, FactQualification, dict[str, DocumentVersion], dict]:
    documents = _latest_documents()
    contents = {
        source_id: (settings.RUNTIME_DIR / document.original_path).resolve().read_bytes()
        for source_id, document in documents.items()
    }
    verification = verify_chain_contents(contents)
    scrutiny = Scrutiny.objects.select_for_update().get(stable_id=SCRUTINY_ID)
    prior_text_id = scrutiny.submitted_text_id
    dossier = scrutiny.submitted_text.dossier

    base_text = TextVersion.objects.get(stable_id="SENAT:2025-2026:TEXT:298:SUBMITTED")
    base_text.kind = "texte de commission avant amendements de séance"
    base_text.stage = "première lecture, base de discussion n° 298"
    base_text.relation = TextVersion.Relation.OTHER
    base_text.source_document = documents["SENAT_TEXT_PPL25_298"]
    base_text.save(update_fields=["kind", "stage", "relation", "source_document", "updated_at"])

    submitted, _ = TextVersion.objects.update_or_create(
        stable_id="SENAT:2025-2026:TEXT:298-MODIFIED:SUBMITTED",
        defaults={
            "dossier": dossier,
            "kind": "texte de commission modifié en séance, reconstruction vérifiée",
            "official_title": scrutiny.official_title,
            "version_label": "n° 298 modifié par les amendements adoptés 13, 4, 9 et 12 ; concordant avec TA n° 50",
            "stage": "première lecture, version exacte mise aux voix au scrutin 178",
            "relation": TextVersion.Relation.SUBMITTED,
            "occurred_on": scrutiny.occurred_on,
            "official_url": ADOPTED_TEXT_URL,
            "source_document": documents["SENAT_TEXT_ADOPTED_050"],
        },
    )
    TextVersion.objects.update_or_create(
        stable_id="SENAT:2025-2026:TEXT:50:ADOPTED",
        defaults={
            "dossier": dossier,
            "kind": "texte adopté",
            "official_title": "Texte adopté par le Sénat",
            "version_label": "n° 50 (2025-2026)",
            "stage": "première lecture, texte résultant du scrutin 178",
            "relation": TextVersion.Relation.ADOPTED,
            "occurred_on": scrutiny.occurred_on,
            "official_url": ADOPTED_TEXT_URL,
            "source_document": documents["SENAT_TEXT_ADOPTED_050"],
        },
    )
    scrutiny.submitted_text = submitted
    scrutiny.source_document = documents["SENAT_SCRUTIN_2025_178"]
    scrutiny.anomaly = ""
    scrutiny.full_clean()
    scrutiny.save(update_fields=["submitted_text", "source_document", "anomaly", "updated_at"])

    references = [
        {
            "source_id": source_id,
            "version_id": document.version_id,
            "sha256": document.sha256,
            "official_url": document.source.document_url,
            "receipt_job_id": document.collection_receipt["job_id"],
        }
        for source_id, document in documents.items()
    ]
    qualification = qualify(
        scrutiny,
        state="qualified",
        object_type="whole_senate_bill_reconstructed_from_adopted_amendments",
        expected_pieces=[],
        references=references,
        explanation=(
            "Le scrutin 178 porte sur le texte de commission 298 modifié. La liste officielle exhaustive "
            "donne les amendements adoptés 13, 4, 9 et 12 ; le compte rendu ordonne leur effet avant le vote ; "
            "le texte adopté 50 concorde avec l'ajout de l'amendement 13 et la suppression de l'article 2."
        ),
    )
    audit_detail = {
        "prior_submitted_text_id": prior_text_id,
        "submitted_text_id": submitted.pk,
        "qualification_id": qualification.pk,
        "qualification_digest": qualification.version_digest,
        "verification_digest": sha256_digest(verification),
        "source_hashes": {source_id: document.sha256 for source_id, document in documents.items()},
        "publication_authorized": False,
        "human_review_created": False,
    }
    audit_identity = {
        "event_type": "senate_exact_act_qualified",
        "object_type": "Scrutiny",
        "object_id": scrutiny.stable_id,
    }
    if not AuditEvent.objects.filter(**audit_identity).exists():
        AuditEvent.objects.create(actor=None, detail=audit_detail, **audit_identity)
    return scrutiny, qualification, documents, verification
