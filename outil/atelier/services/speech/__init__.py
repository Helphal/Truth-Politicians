"""SPEECH-01/02/03 — connecteurs de paroles publiques."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from django.db import transaction
from django.utils import timezone

from atelier.models import (
    Declaration,
    DocumentVersion,
    EvidencePassage,
    Organization,
    Person,
    Source,
)
from atelier.services.canonical import sha256_digest


@dataclass
class SpeechCandidate:
    """Déclaration candidate avant persistance / qualification humaine."""

    stable_id: str
    speaker_label: str
    capacity: str
    exact_quote: str
    occurred_on: date | None
    occurred_on_state: str = "known"
    date_precision: str = "day"
    medium: str = Declaration.Medium.INSTITUTIONAL
    source_role: str = Declaration.SourceRole.ORIGINAL
    position_scope: str = Declaration.PositionScope.INDETERMINATE
    context: str = ""
    source_url: str = ""
    locator: str = ""
    organization_label: str = ""
    person_stable_id: str | None = None
    organization_stable_id: str | None = None
    verification_status: str = "candidate_unverified"
    content_hash: str = ""
    rights_note: str = ""
    # DocumentVersion capturé contenant réellement la citation (détail / Syceron…).
    # Si absent : fallback synthétique text/plain explicitement marqué.
    evidence_document_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def ensure_hash(self) -> str:
        if not self.content_hash:
            self.content_hash = sha256_digest(
                {
                    "quote": self.exact_quote,
                    "url": self.source_url,
                    "locator": self.locator,
                    "speaker": self.speaker_label,
                    "date": self.occurred_on.isoformat() if self.occurred_on else None,
                }
            )
        return self.content_hash


class SpeechConnector(Protocol):
    connector_id: str
    capabilities: set[str]

    def discover(self) -> list[SpeechCandidate]: ...


def resolve_identity(
    *,
    display_name: str,
    institutional_id: str | None = None,
    organization_name: str | None = None,
) -> tuple[Person | None, Organization | None, str]:
    """Résolution stricte : pas de fusion d'homonymes."""
    person = None
    org = None
    status = "unresolved"
    if institutional_id:
        stable = f"person:{institutional_id}"
        person = Person.objects.filter(stable_id=stable).first()
        if person is None:
            # Recherche prudente sans JSON contains (SQLite de test).
            for candidate in Person.objects.exclude(institutional_ids={})[:500]:
                ids = candidate.institutional_ids or {}
                if ids.get("primary") == institutional_id or institutional_id in ids.values():
                    person = candidate
                    break
        if person is None:
            person = Person.objects.create(
                stable_id=stable,
                display_name=display_name,
                institutional_ids={"primary": institutional_id},
                identity_status=Person.IdentityStatus.RESOLVED,
            )
        status = person.identity_status
    else:
        matches = list(Person.objects.filter(display_name__iexact=display_name)[:3])
        if len(matches) == 1:
            person = matches[0]
            status = person.identity_status
        elif len(matches) > 1:
            status = Person.IdentityStatus.CONFLICT
            # Ne fusionne pas : laisse person=None et conserve l'ambiguïté.
            person = None
        else:
            person = Person.objects.create(
                stable_id=f"person:unresolved:{sha256_digest(display_name)[:16]}",
                display_name=display_name,
                identity_status=Person.IdentityStatus.UNRESOLVED,
            )
            status = Person.IdentityStatus.UNRESOLVED
    if organization_name:
        org, _ = Organization.objects.get_or_create(
            stable_id=f"org:{sha256_digest(organization_name.casefold())[:16]}",
            defaults={
                "name": organization_name,
                "kind": Organization.Kind.INSTITUTION,
            },
        )
    return person, org, status


@transaction.atomic
def persist_speech_candidate(candidate: SpeechCandidate, *, source: Source) -> Declaration | None:
    """Crée Passage/Declaration ; rattache un DocumentVersion de preuve existant si fourni."""
    if candidate.source_role == Declaration.SourceRole.RELAY and not candidate.extra.get("relay_acknowledged"):
        # Une reprise journalistique ne devient jamais silencieusement originale.
        candidate.source_role = Declaration.SourceRole.RELAY
    digest = candidate.ensure_hash()
    if Declaration.objects.filter(stable_id=candidate.stable_id).exists():
        return Declaration.objects.get(stable_id=candidate.stable_id)

    evidence_doc = None
    evidence_bound = False
    if candidate.evidence_document_id:
        evidence_doc = DocumentVersion.objects.filter(pk=candidate.evidence_document_id).first()
        if evidence_doc is not None:
            evidence_bound = True

    if evidence_doc is None:
        # Fallback synthétique explicite — jamais présenté comme original capturé.
        evidence_doc, _ = DocumentVersion.objects.get_or_create(
            source=source,
            sha256=digest,
            defaults={
                "version_id": digest[:32],
                "captured_at": timezone.now(),
                "media_type": "text/plain",
                "byte_size": len(candidate.exact_quote.encode("utf-8")),
                "parser_version": "dv-speech/synthetic-fallback/1",
                "collection_receipt": {
                    "execution_mode": "synthetic_fallback",
                    "reason": "no_captured_evidence_document",
                    "not_original_capture": True,
                },
            },
        )
        if not (evidence_doc.collection_receipt or {}).get("not_original_capture"):
            receipt = dict(evidence_doc.collection_receipt or {})
            receipt.update(
                {
                    "execution_mode": receipt.get("execution_mode") or "synthetic_fallback",
                    "not_original_capture": True,
                    "reason": "no_captured_evidence_document",
                }
            )
            if evidence_doc.parser_version.startswith("dv-speech/") and "synthetic" not in evidence_doc.parser_version:
                evidence_doc.parser_version = "dv-speech/synthetic-fallback/1"
            evidence_doc.collection_receipt = receipt
            evidence_doc.save(update_fields=["parser_version", "collection_receipt", "updated_at"])

    # Idempotence passage : même document + locator + texte
    locator = (candidate.locator or candidate.source_url or candidate.stable_id)[:500]
    passage = EvidencePassage.objects.filter(
        document=evidence_doc,
        locator=locator,
        exact_text=candidate.exact_quote,
    ).first()
    if passage is None:
        passage = EvidencePassage.objects.create(
            document=evidence_doc,
            exact_text=candidate.exact_quote,
            text_state="known" if candidate.exact_quote else "unknown",
            transcription_status="source_text" if candidate.exact_quote else "absent",
            locator=locator,
            context=candidate.context,
            public_url=candidate.source_url or source.document_url or source.discovery_url,
        )
    person, org, identity_status = resolve_identity(
        display_name=candidate.speaker_label,
        institutional_id=candidate.person_stable_id,
        organization_name=candidate.organization_label or None,
    )
    if identity_status == Person.IdentityStatus.CONFLICT and person is None:
        # Conserve l'ambiguïté via organisation seule si possible, sinon org placeholder.
        if org is None:
            org, _ = Organization.objects.get_or_create(
                stable_id=f"org:ambiguous:{digest[:12]}",
                defaults={
                    "name": f"Ambiguïté d'identité — {candidate.speaker_label}",
                    "kind": Organization.Kind.INSTITUTION,
                },
            )
    declaration = Declaration.objects.create(
        stable_id=candidate.stable_id,
        speaker_person=person,
        speaker_organization=org,
        speaker_label=candidate.speaker_label,
        capacity=candidate.capacity,
        exact_quote=candidate.exact_quote,
        occurred_on=candidate.occurred_on,
        occurred_on_state=candidate.occurred_on_state,
        date_precision=candidate.date_precision,
        position_scope=candidate.position_scope,
        source_passage=passage,
        medium=candidate.medium,
        source_role=candidate.source_role,
        verification_status=candidate.verification_status,
    )
    candidate.extra["evidence_document_bound"] = evidence_bound
    return declaration


class InstitutionalDebateConnector:
    connector_id = "institutional_debate"
    capabilities = {"debates", "questions", "official_speech"}

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []

    def discover(self) -> list[SpeechCandidate]:
        out = []
        for item in self.items:
            out.append(
                SpeechCandidate(
                    stable_id=item["stable_id"],
                    speaker_label=item["speaker_label"],
                    capacity=item.get("capacity", ""),
                    exact_quote=item["exact_quote"],
                    occurred_on=date.fromisoformat(item["occurred_on"]) if item.get("occurred_on") else None,
                    occurred_on_state=item.get("occurred_on_state", "known"),
                    date_precision=item.get("date_precision", "day"),
                    medium=item.get("medium", Declaration.Medium.INSTITUTIONAL),
                    source_role=item.get("source_role", Declaration.SourceRole.ORIGINAL),
                    position_scope=item.get("position_scope", Declaration.PositionScope.INDETERMINATE),
                    context=item.get("context", ""),
                    source_url=item.get("source_url", ""),
                    locator=item.get("locator", ""),
                    organization_label=item.get("organization_label", ""),
                    person_stable_id=item.get("person_stable_id"),
                    verification_status="institutional_candidate",
                )
            )
        return out


class PressRssConnector:
    connector_id = "press_rss"
    capabilities = {"rss", "atom", "excerpt"}

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []

    def discover(self) -> list[SpeechCandidate]:
        out = []
        for item in self.items:
            role = item.get("source_role", Declaration.SourceRole.RELAY)
            out.append(
                SpeechCandidate(
                    stable_id=item["stable_id"],
                    speaker_label=item["speaker_label"],
                    capacity=item.get("capacity", "cité dans la presse"),
                    exact_quote=item.get("exact_quote", item.get("excerpt", "")),
                    occurred_on=date.fromisoformat(item["occurred_on"]) if item.get("occurred_on") else None,
                    medium=Declaration.Medium.PRESS,
                    source_role=role,
                    context=item.get("context", ""),
                    source_url=item.get("source_url", ""),
                    locator=item.get("locator", ""),
                    verification_status="press_candidate",
                    extra={"relay_acknowledged": role == Declaration.SourceRole.RELAY, "genre": item.get("genre", "reported_quote")},
                )
            )
        return out


class YoutubeMetaConnector:
    connector_id = "youtube_meta"
    capabilities = {"video_metadata", "public_captions"}

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []

    def discover(self) -> list[SpeechCandidate]:
        out = []
        for item in self.items:
            transcript = item.get("transcript") or ""
            out.append(
                SpeechCandidate(
                    stable_id=item["stable_id"],
                    speaker_label=item.get("speaker_label", item.get("channel_title", "inconnu")),
                    capacity=item.get("capacity", "vidéo publique"),
                    exact_quote=transcript or item.get("title", ""),
                    occurred_on=date.fromisoformat(item["occurred_on"]) if item.get("occurred_on") else None,
                    medium=Declaration.Medium.VIDEO,
                    source_role=Declaration.SourceRole.TRANSCRIPT if transcript else Declaration.SourceRole.UNKNOWN,
                    source_url=item.get("source_url", ""),
                    locator=item.get("timecode", ""),
                    verification_status="video_metadata_only" if not transcript else "public_caption",
                    extra={
                        "video_id": item.get("video_id"),
                        "full_video_retained": False,
                        "transcript_absent": not bool(transcript),
                    },
                )
            )
        return out


class PodcastRssConnector:
    connector_id = "podcast_rss"
    capabilities = {"podcast_rss", "public_transcript"}

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []

    def discover(self) -> list[SpeechCandidate]:
        return YoutubeMetaConnector(
            [
                {
                    **item,
                    "medium": Declaration.Medium.RADIO,
                    "channel_title": item.get("show_title", item.get("speaker_label", "")),
                }
                for item in self.items
            ]
        ).discover()


class BlueskyPublicConnector:
    connector_id = "bluesky_public"
    capabilities = {"social_public_api"}

    def __init__(self, items: list[dict] | None = None):
        self.items = items or []

    def discover(self) -> list[SpeechCandidate]:
        out = []
        for item in self.items:
            out.append(
                SpeechCandidate(
                    stable_id=item["stable_id"],
                    speaker_label=item.get("handle", item.get("speaker_label", "")),
                    capacity=item.get("capacity", "compte Bluesky"),
                    exact_quote=item.get("text", ""),
                    occurred_on=date.fromisoformat(item["occurred_on"]) if item.get("occurred_on") else None,
                    medium=Declaration.Medium.SOCIAL,
                    source_role=Declaration.SourceRole.ORIGINAL
                    if not item.get("repost")
                    else Declaration.SourceRole.RELAY,
                    source_url=item.get("source_url", ""),
                    locator=item.get("post_id", ""),
                    verification_status="bluesky_public_candidate",
                    extra={"repost": bool(item.get("repost")), "relay_acknowledged": bool(item.get("repost"))},
                )
            )
        return out


class BlockedSocialConnector:
    connector_id = "social_blocked"
    capabilities = {"contract_only"}

    def __init__(self, provider: str):
        self.provider = provider
        self.connector_id = f"social_blocked:{provider}"

    def discover(self) -> list[SpeechCandidate]:
        return []

    def health(self) -> dict:
        return {
            "provider": self.provider,
            "status": "blocked_credentials_or_provider_terms",
            "account_created": False,
            "token_present": False,
            "mocks_available": True,
        }


class TranscriptionEngine(Protocol):
    def transcribe(self, media_ref: str) -> str | None: ...


class NullTranscriptionEngine:
    """Moteur pluggable : aucun fournisseur payant ; retourne None."""

    def transcribe(self, media_ref: str) -> str | None:
        return None


class FixtureTranscriptionEngine:
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = mapping or {}

    def transcribe(self, media_ref: str) -> str | None:
        return self.mapping.get(media_ref)


CONNECTOR_REGISTRY: dict[str, type] = {
    "institutional_debate": InstitutionalDebateConnector,
    "press_rss": PressRssConnector,
    "youtube_meta": YoutubeMetaConnector,
    "podcast_rss": PodcastRssConnector,
    "bluesky_public": BlueskyPublicConnector,
}
