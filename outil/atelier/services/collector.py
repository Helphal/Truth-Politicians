"""Collecte HTTPS bornée, atomique et résistante aux destinations privées."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import io
import logging
import os
import socket
import ssl
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from atelier.models import DocumentVersion, Source


logger = logging.getLogger("atelier.collector")


class CollectionError(RuntimeError):
    code = "collection_error"


class CollectionBlocked(CollectionError):
    code = "collection_blocked"


class UnsafeDestination(CollectionError):
    code = "unsafe_destination"


class ResponseTooLarge(CollectionError):
    code = "response_too_large"


class UnsafeArchive(CollectionError):
    code = "unsafe_archive"


@dataclass(frozen=True)
class FetchResult:
    document: DocumentVersion
    created: bool
    status: str


class _BufferedProtectedResponse:
    def __init__(self, result):
        self.status = result.status_code
        self._headers = result.headers
        self._content = io.BytesIO(result.content)
        self.transport_receipt = result.receipt
        self.final_url = result.final_url

    def getheader(self, name: str, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, amount: int = -1):
        return self._content.read(amount)


class _BufferedProtectedConnection:
    def __init__(self, response):
        self.response = response

    def close(self):
        self.response._content.close()


def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if not ip.is_global:
        raise UnsafeDestination("destination_non_publique")
    return ip


def resolve_public(host: str, port: int = 443) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CollectionError("dns_failure") from exc
    addresses = sorted({answer[4][0] for answer in answers})
    if not addresses:
        raise CollectionError("dns_empty")
    for address in addresses:
        _public_ip(address)
    return tuple(addresses)


def validate_source_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    resolve_dns: bool = True,
) -> tuple[str, str, tuple[str, ...]]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or host not in (settings.DV_SOURCE_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts)
        ):
            raise UnsafeDestination("url_non_autorisee")
        try:
            _public_ip(host)
        except ValueError:
            pass
        else:
            raise UnsafeDestination("ip_litterale_interdite")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, UnsafeDestination):
            raise
        raise UnsafeDestination("url_invalide") from exc
    return host, path, resolve_public(host) if resolve_dns else ()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, *, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        peer = raw.getpeername()[0]
        if _public_ip(peer) != _public_ip(self._pinned_ip):
            raw.close()
            raise UnsafeDestination("peer_ip_changed")
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _request_once(url: str, headers: dict[str, str], *, timeout: float):
    host, path, addresses = validate_source_url(url)
    last_error = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(host, address, timeout=timeout)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return connection, response
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            connection.close()
            last_error = exc
    raise CollectionError("connection_failed") from last_error


def _safe_storage_root() -> Path:
    root = (settings.RUNTIME_DIR / "raw-snapshots").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def fetch_source(source: Source, *, max_bytes: int = 50 * 1024 * 1024, timeout: float = 30,
                 max_seconds: float = 60, max_redirects: int = 3) -> FetchResult:
    from .network_policy import require_collection_transport, NetworkBlocked
    try:
        transport = require_collection_transport()
    except NetworkBlocked as exc:
        raise CollectionBlocked(str(exc)) from exc
    if not source.operational_enabled or source.access_status not in {Source.AccessStatus.QUALIFIED, Source.AccessStatus.ACTIVE}:
        raise CollectionBlocked("source_not_operational")
    if source.rights_status == Source.RightsStatus.FORBIDDEN:
        raise CollectionBlocked("retention_forbidden")

    previous = source.documents.order_by("-captured_at").first()
    headers = {
        "Accept": "application/json, application/zip, text/html, application/xml, text/xml;q=0.9, */*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "User-Agent": "TruthPoliticians-private-collector/0.1",
    }
    if previous and previous.etag:
        headers["If-None-Match"] = previous.etag
    if previous and previous.last_modified:
        headers["If-Modified-Since"] = previous.last_modified

    current_url = source.document_url or source.discovery_url
    if transport == "protected":
        # Contrôle local de la destination sans résoudre le DNS : la résolution
        # et la preuve de route appartiennent au service protégé.
        validate_source_url(current_url, resolve_dns=False)
    started = time.monotonic()
    connection = None
    response = None
    transport_receipt = {"transport": "direct", "no_implicit_fallback": True}
    try:
        for redirect_count in range(max_redirects + 1):
            if transport == "protected":
                from .protected_transport import ProtectedTransportError, acquire_url
                try:
                    protected = acquire_url(
                        current_url,
                        source_id=source.stable_id,
                        max_bytes=max_bytes,
                        max_seconds=max_seconds,
                    )
                except ProtectedTransportError as exc:
                    raise CollectionBlocked(str(exc)) from exc
                response = _BufferedProtectedResponse(protected)
                connection = _BufferedProtectedConnection(response)
                validate_source_url(protected.final_url, resolve_dns=False)
                current_url = protected.final_url
                transport_receipt = protected.receipt
            else:
                connection, response = _request_once(current_url, headers, timeout=timeout)
            if response.status == 304 and previous:
                source.last_success_at = timezone.now()
                source.last_failure_code = ""
                source.save(update_fields=["last_success_at", "last_failure_code", "updated_at"])
                return FetchResult(previous, False, "not_modified")
            if transport == "direct" and response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                connection.close()
                if not location or redirect_count == max_redirects:
                    raise CollectionError("redirect_limit")
                current_url = urljoin(current_url, location)
                validate_source_url(current_url)
                continue
            if response.status != 200:
                raise CollectionError(f"http_{response.status}")
            break
        else:
            raise CollectionError("redirect_limit")

        length = response.getheader("Content-Length")
        if length and int(length) > max_bytes:
            raise ResponseTooLarge("content_length_exceeded")
        storage_root = _safe_storage_root()
        incoming = settings.RUNTIME_DIR / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(prefix="dv-", suffix=".part", dir=incoming, delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                while True:
                    if time.monotonic() - started > max_seconds:
                        raise CollectionError("duration_limit")
                    block = response.read(min(64 * 1024, max_bytes - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > max_bytes:
                        raise ResponseTooLarge("stream_limit_exceeded")
                    digest.update(block)
                    temp.write(block)
                temp.flush()
                os.fsync(temp.fileno())
            except Exception:
                temp.close()
                temp_path.unlink(missing_ok=True)
                raise
        sha256 = digest.hexdigest()
        suffix = ".zip" if (response.getheader("Content-Type") or "").split(";", 1)[0] == "application/zip" else ".bin"
        safe_source_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in source.stable_id)
        final = (storage_root / f"{safe_source_id}-{sha256}{suffix}").resolve()
        if storage_root not in final.parents:
            temp_path.unlink(missing_ok=True)
            raise CollectionError("storage_escape")
        os.replace(temp_path, final)
        version_id = (response.getheader("ETag") or response.getheader("Last-Modified") or sha256[:20]).strip('"')
        with transaction.atomic():
            document, created = DocumentVersion.objects.get_or_create(
                source=source,
                sha256=sha256,
                defaults={
                    "version_id": version_id[:160],
                    "captured_at": timezone.now(),
                    "media_type": (response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0],
                    "byte_size": size,
                    "original_path": str(final.relative_to(settings.RUNTIME_DIR)),
                    "public_copy_allowed": source.rights_status == Source.RightsStatus.OPEN,
                    "etag": (response.getheader("ETag") or "")[:500],
                    "last_modified": (response.getheader("Last-Modified") or "")[:500],
                    "collection_receipt": transport_receipt,
                },
            )
            if not created:
                updates = []
                if not document.collection_receipt and transport_receipt:
                    # Les versions héritées peuvent déjà épingler les mêmes
                    # octets sans reçu réseau. Le reçu complète la provenance ;
                    # il ne remplace jamais un reçu antérieur.
                    document.collection_receipt = transport_receipt
                    updates.append("collection_receipt")
                prior_path = (settings.RUNTIME_DIR / document.original_path).resolve()
                if settings.RUNTIME_DIR.resolve() not in prior_path.parents or not prior_path.is_file():
                    # La copie de base peut référencer un stockage non transmis.
                    # La nouvelle capture vérifiée des mêmes octets répare alors
                    # seulement le localisateur, jamais l'identité de version.
                    document.original_path = str(final.relative_to(settings.RUNTIME_DIR))
                    updates.append("original_path")
                if updates:
                    document.save(update_fields=[*updates, "updated_at"])
            source.last_success_at = timezone.now()
            source.last_failure_code = ""
            source.access_status = Source.AccessStatus.ACTIVE
            source.save(update_fields=["last_success_at", "last_failure_code", "access_status", "updated_at"])
        if not created and final.exists():
            # Même contenu : un seul original suffit. Le chemin correspond déjà
            # à l'empreinte et n'écrase donc jamais un contenu différent.
            pass
        logger.info("capture_ok source=%s host=%s bytes=%d created=%s", source.stable_id, urlsplit(current_url).hostname, size, created)
        return FetchResult(document, created, "downloaded" if created else "deduplicated")
    except CollectionError as exc:
        source.last_failure_at = timezone.now()
        source.last_failure_code = exc.code if exc.code != "collection_error" else str(exc)[:80]
        source.save(update_fields=["last_failure_at", "last_failure_code", "updated_at"])
        logger.warning("capture_failed source=%s code=%s", source.stable_id, source.last_failure_code)
        raise
    finally:
        if connection is not None:
            connection.close()


def inspect_zip(path: Path, *, max_files: int = 20_000, max_uncompressed: int = 250 * 1024 * 1024,
                max_ratio: int = 200) -> list[zipfile.ZipInfo]:
    path = path.resolve()
    infos = []
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > max_files:
                raise UnsafeArchive("too_many_files")
            for info in infos:
                name = PurePosixPath(info.filename.replace("\\", "/"))
                if name.is_absolute() or ".." in name.parts or not name.parts:
                    raise UnsafeArchive("path_traversal")
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode in {0o120000, 0o060000, 0o020000}:
                    raise UnsafeArchive("special_file")
                total += info.file_size
                if total > max_uncompressed:
                    raise UnsafeArchive("uncompressed_limit")
                if info.compress_size == 0 and info.file_size > 0:
                    raise UnsafeArchive("compression_ratio")
                if info.compress_size and info.file_size / info.compress_size > max_ratio:
                    raise UnsafeArchive("compression_ratio")
    except zipfile.BadZipFile as exc:
        raise UnsafeArchive("bad_zip") from exc
    return infos


def read_zip_member(path: Path, info: zipfile.ZipInfo, *, max_member_bytes: int = 10 * 1024 * 1024) -> bytes:
    if info.file_size > max_member_bytes:
        raise UnsafeArchive("member_too_large")
    with zipfile.ZipFile(path) as archive, archive.open(info) as member:
        data = member.read(max_member_bytes + 1)
    if len(data) > max_member_bytes:
        raise UnsafeArchive("member_too_large")
    return data
