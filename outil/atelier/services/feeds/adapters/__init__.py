"""Adaptateurs FEEDS BUILD-002-R1 — live réel ou fixture offline, jamais de fallback silencieux."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from django.conf import settings
from django.utils import timezone

from atelier.services.feeds.registry import FeedSpec, feed_by_id


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "build002"

# Adaptateurs avec collecte HTTPS via Source.document_url / discovery_url.
LIVE_HTTP_ADAPTERS = frozenset(
    {
        "an_amo",
        "an_votes",
        "an_amendements",
        "an_dossiers",
        "an_cr",
        "an_questions",
        "senat_acteurs",
        "senat_dosleg",
        "senat_ameli",
        "senat_akn",
        "senat_cr",
        "senat_questions",
        "senat_scrutins",
        "elysee_speech",
        "gouv_speech",
        "http_snapshot",
        "legifrance_opendata",
    }
)

CONFIGURABLE_ADAPTERS = frozenset({"press_rss", "youtube_meta", "podcast_rss", "bluesky_public"})
CREDENTIAL_ADAPTERS = frozenset({"legifrance_piste", "social_blocked"})


@dataclass
class AdapterResult:
    status: str  # created | unchanged | blocked | error | fixture
    feed_id: str
    execution_mode: str = "fixture"  # live | fixture | blocked
    etag: str = ""
    last_modified: str = ""
    cursor_token: str = ""
    payload_digest: str = ""
    objects_count: int = 0
    document_id: int | None = None
    parser_id: str = ""
    raw_items: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] | None = None
    error_code: str = ""

    def __post_init__(self):
        if self.detail is None:
            self.detail = {}


class FeedAdapter(Protocol):
    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult: ...


class BlockedCredentialsAdapter:
    """Contrat seulement — pas de transport authentifié implémenté.

    Même si les variables d'environnement sont présentes hors Git, l'adaptateur
    reste bloqué : aucun appel live implicite et aucune promesse « ready ».
    """

    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        health = credential_health(spec)
        return AdapterResult(
            status="blocked",
            feed_id=spec.feed_id,
            execution_mode="blocked",
            error_code="contract_only",
            detail={
                "auth_required": True,
                "requires_env": list(spec.extra.get("requires_env", [])),
                "provider": spec.extra.get("provider", spec.authority),
                "no_account_created": True,
                "no_secret_in_git": True,
                "credentials_present_non_secret": health["credentials_present"],
                "ready_when_credentials_injected": False,
                "implementation_status": "adapter_not_implemented_after_credentials",
                "contract_only": True,
                "health": health,
            },
        )


class ProviderUnavailableAdapter:
    """Fournisseur public inaccessible sans contournement (ex. WAF)."""

    def __init__(self, error_code: str = "provider_unavailable_or_waf"):
        self.error_code = error_code

    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        return AdapterResult(
            status="blocked",
            feed_id=spec.feed_id,
            execution_mode="blocked",
            error_code=self.error_code,
            detail={
                "provider": spec.authority,
                "discovery_url": spec.discovery_url,
                "document_url": spec.document_url,
                "no_waf_bypass": True,
                "public_alternatives_checked": [
                    "https://www.gouvernement.fr/",
                    "https://www.gouvernement.fr/sitemap.xml",
                    "https://www.gouvernement.fr/feed",
                    "https://www.info.gouv.fr/",
                    "https://www.info.gouv.fr/rss",
                ],
                "note": "Seuls robots.txt restent accessibles ; pas de voie officielle exploitable sans contournement",
            },
        )


class ConfigurationIncompleteAdapter:
    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        return AdapterResult(
            status="blocked",
            feed_id=spec.feed_id,
            execution_mode="blocked",
            error_code="configuration_incomplete",
            detail={"message": "cibles non configurées (settings DV_FEED_*)"},
        )


class NotImplementedAdapter:
    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        return AdapterResult(
            status="blocked",
            feed_id=spec.feed_id,
            execution_mode="blocked",
            error_code="blocked_adapter_not_implemented",
            detail={"adapter": spec.adapter},
        )


def _store_fixture_document(spec: FeedSpec, raw: bytes, *, media_type: str = "application/json") -> int:
    from atelier.models import DocumentVersion, Source

    source, _ = Source.objects.get_or_create(
        stable_id=spec.feed_id,
        defaults={
            "producer": spec.authority,
            "family": spec.family,
            "discovery_url": spec.discovery_url or "https://www.assemblee-nationale.fr/",
            "document_url": spec.document_url or "",
            "rights_status": Source.RightsStatus.OPEN,
            "access_status": Source.AccessStatus.QUALIFIED,
            "operational_enabled": True,
            "qualification_notes": "fixture BUILD-002",
        },
    )
    digest = hashlib.sha256(raw).hexdigest()
    doc, _ = DocumentVersion.objects.get_or_create(
        source=source,
        sha256=digest,
        defaults={
            "version_id": digest[:32],
            "captured_at": timezone.now(),
            "media_type": media_type,
            "byte_size": len(raw),
            "parser_version": f"fixture:{spec.adapter}",
            "collection_receipt": {"execution_mode": "fixture", "feed_id": spec.feed_id},
        },
    )
    return doc.pk


def _store_live_document(
    spec: FeedSpec,
    raw: bytes,
    *,
    media_type: str = "application/json",
    provenance: dict | None = None,
) -> int:
    """Persiste un snapshot live avec provenance live cohérente (pas de marqueur fixture)."""
    from atelier.models import DocumentVersion, Source
    from django.conf import settings as dj_settings

    source, _ = Source.objects.get_or_create(
        stable_id=spec.feed_id,
        defaults={
            "producer": spec.authority,
            "family": spec.family,
            "discovery_url": spec.discovery_url or "https://www.assemblee-nationale.fr/",
            "document_url": spec.document_url or "",
            "rights_status": Source.RightsStatus.OPEN,
            "access_status": Source.AccessStatus.QUALIFIED,
            "operational_enabled": True,
            "qualification_notes": "live BUILD-002-R2",
        },
    )
    digest = hashlib.sha256(raw).hexdigest()
    runtime = Path(dj_settings.RUNTIME_DIR)
    snap_dir = runtime / "raw-snapshots" / "feeds-live"
    snap_dir.mkdir(parents=True, exist_ok=True)
    rel = Path("raw-snapshots") / "feeds-live" / f"{spec.feed_id.lower()}-{digest[:24]}.bin"
    full = (runtime / rel).resolve()
    if runtime.resolve() in full.parents and not full.exists():
        full.write_bytes(raw)
    receipt = {
        "execution_mode": "live",
        "feed_id": spec.feed_id,
        "adapter": spec.adapter,
        "provenance": provenance or {},
    }
    doc, created = DocumentVersion.objects.get_or_create(
        source=source,
        sha256=digest,
        defaults={
            "version_id": digest[:32],
            "captured_at": timezone.now(),
            "media_type": media_type,
            "byte_size": len(raw),
            "original_path": str(rel).replace("\\", "/"),
            "parser_version": f"live:{spec.adapter}",
            "collection_receipt": receipt,
        },
    )
    if not created:
        updates = []
        if doc.parser_version.startswith("fixture:") or (doc.collection_receipt or {}).get("execution_mode") == "fixture":
            doc.parser_version = f"live:{spec.adapter}"
            doc.collection_receipt = receipt
            updates.extend(["parser_version", "collection_receipt"])
        if not doc.original_path:
            doc.original_path = str(rel).replace("\\", "/")
            updates.append("original_path")
        if updates:
            doc.save(update_fields=[*updates, "updated_at"])
    return doc.pk


class FixtureAdapter:
    """Uniquement lorsque DV_NETWORK_MODE=disabled. Persiste un DocumentVersion."""

    def __init__(self, fixture_name: str | None = None):
        self.fixture_name = fixture_name

    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        name = self.fixture_name or f"{spec.feed_id.lower()}.json"
        path = FIXTURE_ROOT / name
        if path.is_file():
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            fixture_label = path.name
        else:
            payload = {
                "feed_id": spec.feed_id,
                "format_note": "synthetic_minimal_fixture",
                "items": [],
                "official_discovery": spec.discovery_url,
            }
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            fixture_label = "synthetic"
        digest = hashlib.sha256(raw).hexdigest()
        new_etag = f'W/"{digest[:16]}"'
        items = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
        if etag and etag == new_etag:
            return AdapterResult(
                status="unchanged",
                feed_id=spec.feed_id,
                execution_mode="fixture",
                etag=etag,
                cursor_token=cursor_token or digest[:24],
                payload_digest=digest,
                objects_count=0,
                parser_id=spec.adapter,
                raw_items=items,
                detail={"reason": "etag_match", "fixture": fixture_label},
            )
        document_id = _store_fixture_document(spec, raw)
        return AdapterResult(
            status="fixture",
            feed_id=spec.feed_id,
            execution_mode="fixture",
            etag=new_etag,
            last_modified=timezone.now().strftime("%a, %d %b %Y %H:%M:%S GMT"),
            cursor_token=digest[:24],
            payload_digest=digest,
            objects_count=len(items),
            document_id=document_id,
            parser_id=spec.adapter,
            raw_items=items,
            detail={"fixture": fixture_label, "network": False},
        )


class HttpSnapshotAdapter:
    """Collecte HTTPS via le collecteur existant (Source opérationnelle)."""

    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        from atelier.models import Source
        from atelier.services.collector import CollectionBlocked, CollectionError, fetch_source
        from atelier.services.network_policy import NetworkBlocked

        source = Source.objects.filter(stable_id=spec.feed_id).first()
        if source is None:
            return AdapterResult(
                status="error",
                feed_id=spec.feed_id,
                execution_mode="live",
                error_code="source_missing",
                detail={"hint": "appeler ensure_feed_cursors avant collect"},
            )
        if not (spec.document_url or spec.discovery_url):
            return AdapterResult(
                status="blocked",
                feed_id=spec.feed_id,
                execution_mode="blocked",
                error_code="configuration_incomplete",
                detail={"message": "document_url et discovery_url absents"},
            )
        try:
            result = fetch_source(source)
        except NetworkBlocked as exc:
            return AdapterResult(
                status="blocked",
                feed_id=spec.feed_id,
                execution_mode="blocked",
                error_code=str(exc),
            )
        except CollectionBlocked as exc:
            return AdapterResult(
                status="blocked",
                feed_id=spec.feed_id,
                execution_mode="live",
                error_code=exc.code or str(exc),
            )
        except CollectionError as exc:
            return AdapterResult(
                status="error",
                feed_id=spec.feed_id,
                execution_mode="live",
                error_code=exc.code or type(exc).__name__,
            )
        # Provenance live cohérente sur le DocumentVersion capturé.
        doc = result.document
        receipt = dict(doc.collection_receipt or {})
        receipt["execution_mode"] = "live"
        receipt["feed_id"] = spec.feed_id
        updates = []
        if doc.parser_version.startswith("fixture:") or not doc.parser_version:
            doc.parser_version = f"live:{spec.adapter}"
            updates.append("parser_version")
        if (doc.collection_receipt or {}).get("execution_mode") != "live":
            doc.collection_receipt = receipt
            updates.append("collection_receipt")
        if updates:
            doc.save(update_fields=[*updates, "updated_at"])
        return AdapterResult(
            status="created" if result.created else "unchanged",
            feed_id=spec.feed_id,
            execution_mode="live",
            etag=result.document.etag or "",
            last_modified=result.document.last_modified or "",
            cursor_token=result.document.sha256[:24],
            payload_digest=result.document.sha256,
            objects_count=1 if result.created else 0,
            document_id=result.document.pk,
            parser_id=spec.adapter,
            detail={"document_id": result.document.pk, "capture_status": result.status},
        )


class ConfigurableMediaAdapter:
    """RSS / YouTube / Bluesky / podcast lorsque des cibles sont configurées."""

    def collect(self, spec: FeedSpec, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
        from atelier.services.speech.fetchers import fetch_configured_feed

        if settings.DV_NETWORK_MODE == "disabled":
            return FixtureAdapter().collect(spec, cursor_token=cursor_token, etag=etag)
        if not targets_configured(spec):
            return ConfigurationIncompleteAdapter().collect(spec, cursor_token=cursor_token, etag=etag)
        try:
            payload = fetch_configured_feed(spec)
        except Exception as exc:  # noqa: BLE001
            return AdapterResult(
                status="error",
                feed_id=spec.feed_id,
                execution_mode="live",
                error_code=type(exc).__name__,
                detail={"message": str(exc)[:300]},
            )
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        document_id = _store_live_document(
            spec,
            raw,
            media_type="application/json",
            provenance={"configured_targets": True, "fetcher": "speech.fetchers"},
        )
        items = payload.get("items") or []
        return AdapterResult(
            status="created",
            feed_id=spec.feed_id,
            execution_mode="live",
            etag=f'W/"{digest[:16]}"',
            cursor_token=digest[:24],
            payload_digest=digest,
            objects_count=len(items),
            document_id=document_id,
            parser_id=spec.adapter,
            raw_items=items,
            detail={"configured_targets": True, "fetched": True, "snapshot_provenance": "live"},
        )


def targets_configured(spec: FeedSpec) -> bool:
    if spec.adapter == "press_rss":
        return bool(getattr(settings, "DV_FEED_RSS_URLS", None))
    if spec.adapter == "podcast_rss":
        return bool(getattr(settings, "DV_FEED_PODCAST_URLS", None))
    if spec.adapter == "youtube_meta":
        return bool(getattr(settings, "DV_FEED_YOUTUBE_CHANNELS", None))
    if spec.adapter == "bluesky_public":
        return bool(getattr(settings, "DV_FEED_BLUESKY_HANDLES", None))
    return False


def credential_health(spec: FeedSpec) -> dict[str, Any]:
    required = list(spec.extra.get("requires_env", []))
    if spec.adapter == "legifrance_piste" and not required:
        required = ["DV_LEGIFRANCE_CLIENT_ID", "DV_LEGIFRANCE_CLIENT_SECRET"]
    if spec.adapter == "social_blocked":
        provider = spec.extra.get("provider", "social")
        required = [f"DV_{provider.upper()}_CLIENT_ID", f"DV_{provider.upper()}_CLIENT_SECRET"]
    present = {name: bool(os.environ.get(name) or getattr(settings, name, None)) for name in required}
    return {
        "adapter": spec.adapter,
        "required_env_names": required,
        "credentials_present": all(present.values()) if required else False,
        "env_presence_flags": present,
        "secret_values_exposed": False,
    }


def resolve_adapter(spec: FeedSpec) -> FeedAdapter:
    if spec.adapter in CREDENTIAL_ADAPTERS or spec.block_reason == "blocked_credentials_or_provider_terms":
        # Credentials : contrat seulement — pas d'appel live implicite, pas de promesse ready.
        return BlockedCredentialsAdapter()
    if (
        spec.adapter == "gouv_speech"
        or spec.block_reason in {"provider_unavailable_or_waf", "provider_unavailable"}
    ):
        # WAF / absence de voie publique exploitable — pas de bypass.
        if settings.DV_NETWORK_MODE == "disabled":
            return FixtureAdapter()
        return ProviderUnavailableAdapter("provider_unavailable_or_waf")
    if settings.DV_NETWORK_MODE == "disabled":
        return FixtureAdapter()
    if spec.adapter in CONFIGURABLE_ADAPTERS or spec.block_reason == "configuration_incomplete":
        if targets_configured(spec):
            return ConfigurableMediaAdapter()
        return ConfigurationIncompleteAdapter()
    if spec.adapter in LIVE_HTTP_ADAPTERS:
        return HttpSnapshotAdapter()
    return NotImplementedAdapter()


def implementation_label(spec: FeedSpec) -> str:
    if spec.adapter == "gouv_speech" or spec.block_reason in {
        "provider_unavailable_or_waf",
        "provider_unavailable",
    }:
        return "provider_unavailable"
    if spec.adapter in CREDENTIAL_ADAPTERS or spec.block_reason == "blocked_credentials_or_provider_terms":
        return "contract_only"
    if spec.adapter in CONFIGURABLE_ADAPTERS or spec.block_reason == "configuration_incomplete":
        return "config_blocked" if not targets_configured(spec) else "live_adapter"
    if spec.adapter in LIVE_HTTP_ADAPTERS:
        return "live_adapter"
    return "fixture_only"


def collect_feed(feed_id: str, *, cursor_token: str = "", etag: str = "") -> AdapterResult:
    spec = feed_by_id(feed_id)
    adapter = resolve_adapter(spec)
    return adapter.collect(spec, cursor_token=cursor_token, etag=etag)


def parse_rss_items(raw_xml: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw_xml)
    items = []
    for node in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = (node.findtext("title") or node.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = ""
        link_node = node.find("link")
        if link_node is not None:
            link = (link_node.get("href") or (link_node.text or "")).strip()
        if not link:
            link = (node.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()
        description = (
            node.findtext("description")
            or node.findtext("{http://www.w3.org/2005/Atom}summary")
            or node.findtext("{http://www.w3.org/2005/Atom}content")
            or ""
        ).strip()
        pub = (
            node.findtext("pubDate")
            or node.findtext("{http://www.w3.org/2005/Atom}updated")
            or node.findtext("{http://www.w3.org/2005/Atom}published")
            or ""
        ).strip()
        items.append(
            {
                "title": title,
                "source_url": link,
                "excerpt": description[:2000],
                "published": pub,
            }
        )
    return items
