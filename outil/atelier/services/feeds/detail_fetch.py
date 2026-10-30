"""Poursuite discovery → détail (BUILD-002-R4).

Live : fetch HTTPS borné via le collecteur sécurisé, hosts autorisés seulement.
Offline (`DV_NETWORK_MODE=disabled`) : résolution fixture locale uniquement.
Jamais de fixture en mode live.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.utils import timezone

from atelier.models import DocumentVersion, Source
from atelier.services.collector import (
    CollectionBlocked,
    CollectionError,
    ResponseTooLarge,
    UnsafeDestination,
    validate_source_url,
)
from atelier.services.feeds.registry import FeedSpec, feed_by_id
from atelier.services.network_policy import NetworkBlocked, require_collection_transport


logger = logging.getLogger("atelier.feeds.detail_fetch")

DETAIL_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2] / "fixtures" / "build002" / "official_samples" / "details"
)

# Hosts autorisés par famille de parseur (intersection avec DV_SOURCE_ALLOWED_HOSTS).
PARSER_DETAIL_HOSTS: dict[str, frozenset[str]] = {
    "elysee_speech": frozenset({"www.elysee.fr"}),
    "gouv_speech": frozenset({"www.gouvernement.fr"}),
    "senat_cr": frozenset({"www.senat.fr", "data.senat.fr"}),
    "senat_questions": frozenset({"www.senat.fr", "data.senat.fr"}),
    "senat_scrutins": frozenset({"www.senat.fr", "data.senat.fr"}),
    "senat_dosleg": frozenset({"www.senat.fr", "data.senat.fr"}),
    "senat_ameli": frozenset({"www.senat.fr", "data.senat.fr"}),
    "an_cr": frozenset({"www.assemblee-nationale.fr", "data.assemblee-nationale.fr"}),
    "an_questions": frozenset({"www.assemblee-nationale.fr", "data.assemblee-nationale.fr"}),
    "an_dossiers": frozenset({"www.assemblee-nationale.fr", "data.assemblee-nationale.fr"}),
    "an_amendements": frozenset({"www.assemblee-nationale.fr", "data.assemblee-nationale.fr"}),
}


@dataclass
class DetailFetchResult:
    status: str  # fetched | unchanged | fixture | blocked | failed
    url: str
    document_id: int | None = None
    raw: bytes | None = None
    media_type: str = "text/html"
    error_code: str = ""
    detail: dict[str, Any] | None = None


def detail_max_per_run() -> int:
    return int(getattr(settings, "DV_FEED_DETAIL_MAX_PER_RUN", 10))


def detail_max_bytes() -> int:
    return int(getattr(settings, "DV_FEED_DETAIL_MAX_BYTES", 2 * 1024 * 1024))


def allowed_hosts_for_parser(parser_id: str) -> set[str]:
    family = PARSER_DETAIL_HOSTS.get(parser_id) or frozenset()
    global_hosts = set(getattr(settings, "DV_SOURCE_ALLOWED_HOSTS", set()) or set())
    if not family:
        return set()
    return {h.lower() for h in family} & {h.lower() for h in global_hosts}


def resolve_detail(
    url: str,
    *,
    feed_id: str,
    parser_id: str,
    parent_document: DocumentVersion | None,
    title: str = "",
    execution_mode: str = "fixture",
) -> DetailFetchResult:
    """Résout une URL de détail selon le mode d'exécution."""
    network_mode = getattr(settings, "DV_NETWORK_MODE", "disabled")
    if execution_mode == "live" or network_mode != "disabled":
        # Live : jamais de fixture.
        if network_mode == "disabled":
            return DetailFetchResult(
                status="blocked",
                url=url,
                error_code="fixture_lookup_forbidden_in_live",
                detail={"reason": "execution_mode_live_but_network_disabled"},
            )
        return _fetch_detail_live(
            url,
            feed_id=feed_id,
            parser_id=parser_id,
            parent_document=parent_document,
        )
    # Offline / fixture uniquement
    raw = lookup_detail_fixture(parser_id, url, title)
    if raw is None:
        return DetailFetchResult(status="failed", url=url, error_code="fixture_detail_missing")
    doc_id = _store_detail_document(
        feed_id=feed_id,
        parser_id=parser_id,
        url=url,
        raw=raw,
        media_type="text/html",
        parent_document=parent_document,
        execution_mode="fixture",
        provenance={"resolver": "fixture_lookup"},
    )
    return DetailFetchResult(
        status="fixture",
        url=url,
        document_id=doc_id,
        raw=raw,
        media_type="text/html",
    )


def lookup_detail_fixture(parser_id: str, url: str, title: str = "") -> bytes | None:
    if not DETAIL_FIXTURE_ROOT.is_dir():
        return None
    map_path = DETAIL_FIXTURE_ROOT / "URL_MAP.json"
    if map_path.is_file():
        try:
            mapping = json.loads(map_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}
        rel = mapping.get(url) or mapping.get(title)
        if rel:
            target = DETAIL_FIXTURE_ROOT / rel
            if target.is_file():
                return target.read_bytes()
    slug = re.sub(r"\W+", "-", (url or title).lower())[-80:]
    for path in DETAIL_FIXTURE_ROOT.glob("*"):
        if not path.is_file() or path.name == "URL_MAP.json":
            continue
        name = path.name.lower()
        if parser_id.startswith("senat") and "senat" in name and "detail" in name:
            if url and Path(url).name.replace(".", "-") in name:
                return path.read_bytes()
        if parser_id.startswith("elysee") and "elysee" in name and "detail" in name:
            if url and any(part and part in name for part in Path(url).parts[-2:]):
                return path.read_bytes()
            if slug and slug[:24] in name:
                return path.read_bytes()
    return None


def _fetch_detail_live(
    url: str,
    *,
    feed_id: str,
    parser_id: str,
    parent_document: DocumentVersion | None,
) -> DetailFetchResult:
    allowed = allowed_hosts_for_parser(parser_id)
    if not allowed:
        return DetailFetchResult(
            status="blocked",
            url=url,
            error_code="detail_hosts_not_configured",
            detail={"parser_id": parser_id},
        )
    try:
        host, _path, _addrs = validate_source_url(url, allowed_hosts=allowed, resolve_dns=False)
    except UnsafeDestination as exc:
        return DetailFetchResult(
            status="blocked",
            url=url,
            error_code="host_not_allowed",
            detail={"error": str(exc), "host": urlsplit(url).hostname},
        )

    try:
        require_collection_transport()
    except NetworkBlocked as exc:
        return DetailFetchResult(
            status="blocked",
            url=url,
            error_code="network_blocked",
            detail={"error": str(exc)},
        )

    try:
        raw, media_type, final_url, http_meta = _http_get_bounded(
            url,
            allowed_hosts=allowed,
            max_bytes=detail_max_bytes(),
            max_redirects=2,
        )
    except UnsafeDestination as exc:
        return DetailFetchResult(
            status="blocked",
            url=url,
            error_code="redirect_forbidden" if "redirect" in str(exc).lower() or "url_non" in str(exc) else "host_not_allowed",
            detail={"error": str(exc)},
        )
    except ResponseTooLarge:
        return DetailFetchResult(status="failed", url=url, error_code="detail_too_large")
    except (CollectionBlocked, CollectionError, OSError, NetworkBlocked) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        return DetailFetchResult(
            status="failed",
            url=url,
            error_code=str(code)[:80],
            detail={"error": str(exc)[:300]},
        )

    digest = hashlib.sha256(raw).hexdigest()
    # Idempotence : même SHA déjà stocké pour ce feed → unchanged
    source = Source.objects.filter(stable_id=feed_id).first()
    existing = None
    if source is not None:
        existing = DocumentVersion.objects.filter(source=source, sha256=digest).first()
    if existing is not None:
        return DetailFetchResult(
            status="unchanged",
            url=final_url or url,
            document_id=existing.pk,
            raw=raw,
            media_type=media_type,
            detail={"sha256": digest, **http_meta},
        )

    doc_id = _store_detail_document(
        feed_id=feed_id,
        parser_id=parser_id,
        url=final_url or url,
        raw=raw,
        media_type=media_type,
        parent_document=parent_document,
        execution_mode="live",
        provenance={
            "resolver": "live_https",
            "requested_url": url,
            "final_url": final_url or url,
            "host": host,
            **http_meta,
        },
    )
    return DetailFetchResult(
        status="fetched",
        url=final_url or url,
        document_id=doc_id,
        raw=raw,
        media_type=media_type,
        detail={"sha256": digest, **http_meta},
    )


def _http_get_bounded(
    url: str,
    *,
    allowed_hosts: set[str],
    max_bytes: int,
    max_redirects: int = 2,
    timeout: float = 30,
) -> tuple[bytes, str, str, dict]:
    """GET HTTPS borné ; valide chaque redirect contre allowed_hosts."""
    from atelier.services.collector import _request_once

    current = url
    headers = {
        "Accept": "text/html, application/xhtml+xml, application/xml;q=0.9, */*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "User-Agent": "TruthPoliticians-private-collector/0.1-detail",
    }
    for redirect_count in range(max_redirects + 1):
        validate_source_url(current, allowed_hosts=allowed_hosts, resolve_dns=True)
        connection, response = _request_once(current, headers, timeout=timeout)
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                connection.close()
                if not location or redirect_count == max_redirects:
                    raise CollectionError("redirect_limit")
                nxt = urljoin(current, location)
                # Interdit redirect hors hosts autorisés
                try:
                    validate_source_url(nxt, allowed_hosts=allowed_hosts, resolve_dns=False)
                except UnsafeDestination as exc:
                    raise UnsafeDestination("redirect_host_not_allowed") from exc
                current = nxt
                continue
            if response.status != 200:
                raise CollectionError(f"http_{response.status}")
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ResponseTooLarge("content_length_exceeded")
            chunks: list[bytes] = []
            size = 0
            while True:
                block = response.read(min(64 * 1024, max_bytes - size + 1))
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise ResponseTooLarge("stream_limit_exceeded")
                chunks.append(block)
            media = (response.getheader("Content-Type") or "text/html").split(";", 1)[0]
            meta = {
                "http_status": response.status,
                "bytes": size,
                "etag": (response.getheader("ETag") or "")[:200],
            }
            return b"".join(chunks), media, current, meta
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
    raise CollectionError("redirect_limit")


def _store_detail_document(
    *,
    feed_id: str,
    parser_id: str,
    url: str,
    raw: bytes,
    media_type: str,
    parent_document: DocumentVersion | None,
    execution_mode: str,
    provenance: dict,
) -> int:
    from atelier.services.feeds.adapters import _store_live_document, _store_fixture_document

    spec = feed_by_id(feed_id)
    if spec is None:
        spec = FeedSpec(
            feed_id=feed_id,
            authority="detail",
            family="detail",
            cadence="manual",
            mechanism="detail_fetch",
            discovery_url=url,
            document_url=url,
            rights_status="open",
            coverage_note="detail_page",
            default_active=False,
            adapter=parser_id,
        )
    parent_meta = {}
    if parent_document is not None:
        parent_meta = {
            "parent_document_id": parent_document.pk,
            "parent_sha256": parent_document.sha256,
            "parent_source_id": parent_document.source_id,
        }
    extra = {
        "detail_url": url,
        "stage": "detail_fetch",
        "depth": 1,
        **parent_meta,
        **(provenance or {}),
    }
    if execution_mode == "live":
        doc_id = _store_live_document(spec, raw, media_type=media_type, provenance=extra)
    else:
        doc_id = _store_fixture_document(spec, raw, media_type=media_type)
        doc = DocumentVersion.objects.get(pk=doc_id)
        receipt = dict(doc.collection_receipt or {})
        receipt.update({"execution_mode": "fixture", "stage": "detail_fetch", **extra})
        doc.collection_receipt = receipt
        doc.save(update_fields=["collection_receipt", "updated_at"])
    return doc_id
