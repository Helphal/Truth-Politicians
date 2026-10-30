"""Client borné d'un service de collecte protégé explicitement configuré."""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from django.conf import settings


class ProtectedTransportError(RuntimeError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    """Les appels privés Atelier→OSINT ne sont jamais redirigeables."""

    handler_order = 100

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ProtectedFetch:
    content: bytes
    status_code: int
    headers: dict[str, str]
    requested_url: str
    final_url: str
    receipt: dict


def _endpoint() -> str:
    value = settings.DV_PROTECTED_TRANSPORT_ENDPOINT.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.path not in {"", "/"})
    ):
        raise ProtectedTransportError("protected_transport_endpoint_invalid")
    return value


def _opener():
    # Aucun proxy d'environnement implicite : le service de collecte protégé porte lui-même le
    # profil TOR/VPN et refuse tout repli.
    return build_opener(
        ProxyHandler({}),
        _RejectRedirects(),
        HTTPSHandler(context=ssl.create_default_context()),
    )


def _same_private_origin(url: str) -> bool:
    expected = urlsplit(_endpoint())
    actual = urlsplit(url)
    return (
        actual.scheme == expected.scheme
        and actual.hostname == expected.hostname
        and actual.port == expected.port
        and actual.username is None
        and actual.password is None
        and not actual.fragment
    )


def _allowed_source_url(url: object) -> str:
    if not isinstance(url, str):
        raise ProtectedTransportError("protected_transport_source_url_invalid")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or host not in settings.DV_SOURCE_ALLOWED_HOSTS
        ):
            raise ProtectedTransportError("protected_transport_source_url_not_allowed")
    except (ValueError, UnicodeError) as exc:
        raise ProtectedTransportError("protected_transport_source_url_invalid") from exc
    return url


def _json_request(url: str, *, method: str = "GET", payload: dict | None = None) -> tuple[dict, int, dict]:
    if not _same_private_origin(url):
        raise ProtectedTransportError("protected_transport_private_origin_mismatch")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TruthPoliticians-private-collector/0.2",
        },
    )
    try:
        with _opener().open(request, timeout=settings.DV_PROTECTED_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ProtectedTransportError("protected_transport_response_too_large")
            return json.loads(raw.decode("utf-8")), response.status, dict(response.headers.items())
    except HTTPError as exc:
        raise ProtectedTransportError(f"protected_transport_http_{exc.code}") from exc
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise ProtectedTransportError("protected_transport_unreachable_no_direct_fallback") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedTransportError("protected_transport_invalid_json") from exc


def _artifact(url: str, *, max_bytes: int) -> bytes:
    if not _same_private_origin(url):
        raise ProtectedTransportError("protected_transport_private_origin_mismatch")
    request = Request(url, method="GET", headers={"Accept": "application/octet-stream"})
    try:
        with _opener().open(request, timeout=settings.DV_PROTECTED_REQUEST_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ProtectedTransportError("protected_transport_artifact_too_large")
            raw = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise ProtectedTransportError(f"protected_transport_artifact_http_{exc.code}") from exc
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise ProtectedTransportError("protected_transport_artifact_unreachable") from exc
    if len(raw) > max_bytes:
        raise ProtectedTransportError("protected_transport_artifact_too_large")
    return raw


def _safe_error_code(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "unknown"))[:60]
    return normalized or "unknown"


def acquire_url(url: str, *, source_id: str, max_bytes: int, max_seconds: float) -> ProtectedFetch:
    """Crée/rejoue un job OSINT, attend sa fin et vérifie l'original reçu."""
    endpoint = _endpoint()
    _allowed_source_url(url)
    profile = settings.DV_PROTECTED_EGRESS_PROFILE
    date_bucket = datetime.now(UTC).strftime("%Y%m%d")
    identity = hashlib.sha256(f"{source_id}\n{url}\n{profile}\n{date_bucket}".encode()).hexdigest()[:40]
    base_payload = {
            "kind": "acquire.url",
            "egress_profile": profile,
            "scope": {
                "project": "truth-politicians",
                "purpose": "official_source_capture",
            },
            "input": {"url": url},
    }
    deadline = time.monotonic() + max_seconds

    def run_job(*, retry: bool) -> tuple[dict, str]:
        payload = {**base_payload, "idempotency_key": f"dv-source-{identity}{'-retry1' if retry else ''}"}
        current, response_status, _ = _json_request(f"{endpoint}/api/v1/jobs", method="POST", payload=payload)
        if response_status not in {200, 201, 202}:
            raise ProtectedTransportError("protected_transport_job_response_invalid")
        current_id = str(current.get("id", ""))
        if not re.fullmatch(r"[0-9a-f-]{36}", current_id):
            raise ProtectedTransportError("protected_transport_job_id_invalid")
        while str(current.get("status")) in {"QUEUED", "RUNNING"}:
            if time.monotonic() >= deadline:
                raise ProtectedTransportError("protected_transport_job_timeout")
            time.sleep(settings.DV_PROTECTED_POLL_SECONDS)
            current, response_status, _ = _json_request(f"{endpoint}/api/v1/jobs/{current_id}")
            if response_status != 200 or str(current.get("id", "")) != current_id:
                raise ProtectedTransportError("protected_transport_job_response_invalid")
        return current, current_id

    job, job_id = run_job(retry=False)
    retried = False
    if str(job.get("status")) not in {"SUCCEEDED", "PARTIAL"}:
        job, job_id = run_job(retry=True)
        retried = True
    if str(job.get("status")) != "SUCCEEDED":
        code = _safe_error_code(job.get("error_code"))
        raise ProtectedTransportError(f"protected_transport_job_{code}")
    receipt = (job.get("checkpoint") or {}).get("receipt") or {}
    if not isinstance(receipt, dict):
        raise ProtectedTransportError("protected_transport_receipt_invalid")
    requested_url = _allowed_source_url(receipt.get("requested_url"))
    final_url = _allowed_source_url(receipt.get("final_url"))
    if requested_url != url:
        raise ProtectedTransportError("protected_transport_requested_url_mismatch")
    redirect_chain = receipt.get("redirect_chain")
    if final_url != requested_url:
        if (
            not isinstance(redirect_chain, list)
            or len(redirect_chain) < 2
            or redirect_chain[0] != requested_url
            or redirect_chain[-1] != final_url
        ):
            raise ProtectedTransportError("protected_transport_redirect_chain_invalid")
        for redirect_url in redirect_chain:
            _allowed_source_url(redirect_url)
    elif redirect_chain not in (None, [], [requested_url]):
        raise ProtectedTransportError("protected_transport_redirect_chain_invalid")
    if type(receipt.get("complete")) is not bool or receipt["complete"] is not True:
        raise ProtectedTransportError("protected_transport_receipt_incomplete")
    reported_profile = receipt.get("egress_profile")
    if job.get("egress_profile") != profile or reported_profile != profile:
        raise ProtectedTransportError("protected_transport_egress_profile_mismatch")
    if type(receipt.get("no_direct_fallback")) is not bool or receipt["no_direct_fallback"] is not True:
        raise ProtectedTransportError("protected_transport_route_proof_missing")
    if type(receipt.get("status_code")) is not int or not 100 <= receipt["status_code"] <= 599:
        raise ProtectedTransportError("protected_transport_status_invalid")
    artifact_id = str(receipt.get("original_artifact_id", ""))
    if not re.fullmatch(r"[0-9a-f-]{36}", artifact_id):
        raise ProtectedTransportError("protected_transport_original_missing")
    content = _artifact(f"{endpoint}/api/v1/artifacts/{artifact_id}", max_bytes=max_bytes)
    expected_sha = str(receipt.get("content_sha256", ""))
    expected_size = receipt.get("size_bytes")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or type(expected_size) is not int or expected_size < 0:
        raise ProtectedTransportError("protected_transport_artifact_metadata_invalid")
    if hashlib.sha256(content).hexdigest() != expected_sha or len(content) != expected_size:
        raise ProtectedTransportError("protected_transport_artifact_digest_mismatch")
    headers = {str(key).lower(): str(value) for key, value in (receipt.get("headers") or {}).items()}
    headers["content-length"] = str(len(content))
    summary = {
        "transport": "protected_capture",
        "egress_profile": reported_profile,
        "requested_egress_profile": profile,
        "reported_egress_profile": reported_profile,
        "route_proof_level": "service_receipt",
        "job_id": job_id,
        "job_status": str(job.get("status")),
        "requested_url": requested_url,
        "final_url": final_url,
        "redirect_chain": redirect_chain or [requested_url],
        "status_code": int(receipt.get("status_code", 0)),
        "content_sha256": expected_sha,
        "size_bytes": expected_size,
        "original_artifact_id": artifact_id,
        "warc_artifact_id": str(receipt.get("warc_artifact_id", "")),
        "warc_integrity": receipt.get("warc_integrity") or {},
        "complete": receipt["complete"],
        "bounded_retry_used": retried,
        "no_direct_fallback": receipt["no_direct_fallback"],
    }
    return ProtectedFetch(
        content=content,
        status_code=summary["status_code"],
        headers=headers,
        requested_url=summary["requested_url"],
        final_url=summary["final_url"],
        receipt=summary,
    )
