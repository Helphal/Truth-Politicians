"""Transport WordPress TEST vers loopback littéral uniquement — jamais activable en production."""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from django.conf import settings

from .canonical import canonical_bytes
from .wordpress import WordPressUnavailable

DEFAULT_LOOPBACK_VIRTUAL_HOST = "truth-politicians.test"
LOOPBACK_CONNECT_ADDR = "127.0.0.1"
MAX_LOOPBACK_BODY_BYTES = 1024 * 1024
MAX_LOOPBACK_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LoopbackTarget:
    connect_addr: str
    port: int
    path: str
    host_header: str
    use_tls: bool


def loopback_transport_enabled() -> bool:
    if settings.DV_ENV not in {"local", "test"}:
        return False
    return os.environ.get("DV_WORDPRESS_LOOPBACK_TEST", "").strip() == "1"


def _configured_virtual_host() -> str:
    return (
        os.environ.get("DV_WORDPRESS_LOOPBACK_VIRTUAL_HOST", DEFAULT_LOOPBACK_VIRTUAL_HOST).strip().lower().rstrip(".")
    )


def _loopback_host_header(virtual_host: str, port: int, use_tls: bool) -> str:
    """Host HTTP aligné sur siteurl WordPress (port non standard ex. Local B :10040)."""
    default_port = 443 if use_tls else 80
    if port == default_port:
        return virtual_host
    return f"{virtual_host}:{port}"


def _allowed_ports() -> frozenset[int]:
    raw = os.environ.get("DV_WORDPRESS_LOOPBACK_CONNECT_PORTS", "80,443,10080,10040")
    ports: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            ports.add(int(item))
    if os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR", "").strip() == "1":
        from .loopback_fault_injector import fault_server_port

        ports.add(fault_server_port())
    return frozenset(ports or {80, 443})


def resolve_loopback_endpoint(endpoint: str) -> LoopbackTarget:
    """Valide l'URL sans résolution DNS ; connexion toujours sur 127.0.0.1."""
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password:
        raise WordPressUnavailable("loopback_url_userinfo_forbidden")
    if parsed.scheme not in {"http", "https"}:
        raise WordPressUnavailable("loopback_scheme_forbidden")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise WordPressUnavailable("loopback_host_forbidden")
    virtual = _configured_virtual_host()
    if host.endswith(".local") and host != virtual:
        raise WordPressUnavailable("loopback_host_forbidden")
    if host not in {LOOPBACK_CONNECT_ADDR, "localhost", virtual}:
        raise WordPressUnavailable("loopback_host_forbidden")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in _allowed_ports():
        raise WordPressUnavailable("loopback_port_forbidden")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if not path.startswith("/"):
        raise WordPressUnavailable("loopback_path_forbidden")
    if host in {LOOPBACK_CONNECT_ADDR, "localhost", virtual}:
        host_header = _loopback_host_header(virtual, port, parsed.scheme == "https")
    else:
        host_header = host
    return LoopbackTarget(
        connect_addr=LOOPBACK_CONNECT_ADDR,
        port=port,
        path=path,
        host_header=host_header,
        use_tls=parsed.scheme == "https",
    )


def _loopback_auth_headers() -> dict[str, str]:
    user = os.environ.get("DV_LOCAL_B_WP_USER", "").strip()
    password = os.environ.get("DV_LOCAL_B_WP_PASSWORD", "")
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def validate_loopback_packet(packet: dict) -> None:
    if not isinstance(packet, dict):
        raise WordPressUnavailable("loopback_packet_invalid")
    if str(packet.get("environment")) != "test":
        raise WordPressUnavailable("loopback_packet_environment_forbidden")
    if str(packet.get("destination")) != "dv-site:test":
        raise WordPressUnavailable("loopback_packet_destination_forbidden")
    if str(packet.get("operation")) == "suspend":
        return
    body = packet.get("payload")
    if not isinstance(body, dict):
        raise WordPressUnavailable("loopback_packet_payload_invalid")
    if body.get("data_scope") != "synthetic":
        raise WordPressUnavailable("loopback_packet_scope_forbidden")


def _pop_loopback_injected_fault() -> str | None:
    if os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR", "").strip() != "1":
        return None
    queue = os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_QUEUE", "").strip()
    if not queue:
        return None
    parts = [p.strip() for p in queue.split(",") if p.strip()]
    if not parts:
        return None
    head, *tail = parts
    os.environ["DV_WORDPRESS_LOOPBACK_FAULT_QUEUE"] = ",".join(tail)
    return head


def reset_loopback_fault_queue() -> None:
    os.environ.pop("DV_WORDPRESS_LOOPBACK_FAULT_QUEUE", None)


def enqueue_loopback_faults(*faults: str) -> None:
    if os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR", "").strip() != "1":
        raise WordPressUnavailable("loopback_fault_injector_disabled")
    existing = os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_QUEUE", "").strip()
    merged = ",".join([existing, *faults]) if existing else ",".join(faults)
    os.environ["DV_WORDPRESS_LOOPBACK_FAULT_QUEUE"] = merged


def _raw_loopback_http_post(
    target: LoopbackTarget,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    opener: Callable[..., http.client.HTTPConnection] | None = None,
) -> tuple[int, bytes]:
    if len(body) > MAX_LOOPBACK_BODY_BYTES:
        raise WordPressUnavailable("loopback_body_too_large")
    factory = opener or (http.client.HTTPSConnection if target.use_tls else http.client.HTTPConnection)
    conn = factory(target.connect_addr, target.port, timeout=timeout)
    try:
        conn.request("POST", target.path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_LOOPBACK_RESPONSE_BYTES + 1)
        if len(raw) > MAX_LOOPBACK_RESPONSE_BYTES:
            raise WordPressUnavailable("loopback_response_too_large")
        return response.status, raw
    except socket.timeout as exc:
        raise WordPressUnavailable("loopback_timeout") from exc
    except OSError as exc:
        raise WordPressUnavailable("loopback_connection_failed") from exc
    finally:
        conn.close()


def _injector_target_for_fault(fault: str) -> LoopbackTarget:
    from .loopback_fault_injector import ensure_fault_server_running, fault_server_port

    ensure_fault_server_running()
    port = fault_server_port()
    if fault == "http429":
        path = "/dv-loopback-fault/429"
    elif fault == "http_timeout":
        path = "/dv-loopback-fault/timeout"
    else:
        raise WordPressUnavailable("loopback_fault_unknown")
    return LoopbackTarget(
        connect_addr=LOOPBACK_CONNECT_ADDR,
        port=port,
        path=path,
        host_header="dv-loopback-fault.test",
        use_tls=False,
    )


def _loopback_http_post(
    target: LoopbackTarget,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    opener: Callable[..., http.client.HTTPConnection] | None = None,
) -> tuple[int, bytes]:
    if len(body) > MAX_LOOPBACK_BODY_BYTES:
        raise WordPressUnavailable("loopback_body_too_large")
    fault = _pop_loopback_injected_fault()
    if fault in {"429", "simulate429"}:
        return 429, b'{"code":"dv_import_rejected","message":"rate_limit_exceeded","data":{"status":429,"simulated":true}}'
    if fault == "timeout":
        raise WordPressUnavailable("loopback_timeout")
    if fault in {"http429", "http_timeout"}:
        inj = _injector_target_for_fault(fault)
        return _raw_loopback_http_post(inj, body=body, headers=headers, timeout=timeout, opener=opener)
    if fault == "invalid_json":
        return 200, b"not-json"
    return _raw_loopback_http_post(
        target, body=body, headers=headers, timeout=timeout, opener=opener
    )


def _loopback_http_get(
    target: LoopbackTarget,
    *,
    headers: dict[str, str],
    timeout: float,
    opener: Callable[..., http.client.HTTPConnection] | None = None,
) -> tuple[int, bytes]:
    factory = opener or (http.client.HTTPSConnection if target.use_tls else http.client.HTTPConnection)
    conn = factory(target.connect_addr, target.port, timeout=timeout)
    try:
        conn.request("GET", target.path, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_LOOPBACK_RESPONSE_BYTES + 1)
        if len(raw) > MAX_LOOPBACK_RESPONSE_BYTES:
            raise WordPressUnavailable("loopback_response_too_large")
        return response.status, raw
    except socket.timeout as exc:
        raise WordPressUnavailable("loopback_timeout") from exc
    except OSError as exc:
        raise WordPressUnavailable("loopback_connection_failed") from exc
    finally:
        conn.close()


def fetch_loopback_raw(
    endpoint: str,
    *,
    accept: str = "application/json",
    timeout: float = 20,
    opener=None,
) -> tuple[int, bytes]:
    if not loopback_transport_enabled():
        raise WordPressUnavailable("loopback_transport_not_enabled")
    target = resolve_loopback_endpoint(endpoint)
    headers = {
        "Accept": accept,
        "Host": target.host_header,
        "User-Agent": "TruthPoliticians-loopback-test/0.1",
        "Connection": "close",
        **_loopback_auth_headers(),
    }
    return _loopback_http_get(target, headers=headers, timeout=timeout, opener=opener)


def fetch_loopback_html(endpoint: str, *, timeout: float = 20, opener=None) -> tuple[int, str]:
    status, raw = fetch_loopback_raw(endpoint, accept="text/html", timeout=timeout, opener=opener)
    return status, raw.decode("utf-8", errors="replace")


def fetch_rest_loopback(endpoint: str, *, timeout: float = 20, opener=None) -> dict:
    if not loopback_transport_enabled():
        raise WordPressUnavailable("loopback_transport_not_enabled")
    status, raw = fetch_loopback_raw(endpoint, timeout=timeout, opener=opener)
    if status not in {200, 201}:
        snippet = raw.decode("utf-8", errors="replace")[:400]
        raise WordPressUnavailable(f"loopback_http_{status}:{snippet}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise WordPressUnavailable("loopback_response_invalid")
    return parsed


def post_rest_loopback(
    endpoint: str,
    body: dict,
    *,
    idempotency_key: str,
    timeout: float = 20,
    opener: Callable[..., http.client.HTTPConnection] | None = None,
) -> dict:
    if not loopback_transport_enabled():
        raise WordPressUnavailable("loopback_transport_not_enabled")
    if not endpoint.strip():
        raise WordPressUnavailable("loopback_endpoint_missing")
    if not idempotency_key:
        raise WordPressUnavailable("loopback_idempotency_missing")
    target = resolve_loopback_endpoint(endpoint)
    payload = canonical_bytes(body)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": idempotency_key,
        "Host": target.host_header,
        "User-Agent": "TruthPoliticians-loopback-test/0.1",
        "Connection": "close",
        **_loopback_auth_headers(),
    }
    status, raw = _loopback_http_post(target, body=payload, headers=headers, timeout=timeout, opener=opener)
    if status == 429:
        raise WordPressUnavailable("loopback_http_429")
    if status not in {200, 201, 202}:
        snippet = raw.decode("utf-8", errors="replace")[:400]
        raise WordPressUnavailable(f"loopback_http_{status}:{snippet}")
    try:
        parsed_body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WordPressUnavailable("loopback_response_invalid_json") from exc
    if not isinstance(parsed_body, dict):
        raise WordPressUnavailable("loopback_response_invalid")
    if os.environ.get("DV_WORDPRESS_LOOPBACK_TEST") == "1":
        parsed_body["_loopback_http_status"] = status
    return parsed_body


def post_packet_loopback(
    packet: dict,
    *,
    timeout: float = 20,
    opener: Callable[..., http.client.HTTPConnection] | None = None,
) -> dict:
    if not loopback_transport_enabled():
        raise WordPressUnavailable("loopback_transport_not_enabled")
    endpoint = os.environ.get("DV_WORDPRESS_LOOPBACK_ENDPOINT", "").strip()
    if not endpoint:
        raise WordPressUnavailable("loopback_endpoint_missing")
    validate_loopback_packet(packet)
    target = resolve_loopback_endpoint(endpoint)
    event_id = str(packet.get("event_id") or "")
    if not event_id:
        raise WordPressUnavailable("loopback_packet_missing_event_id")
    body = canonical_bytes(packet)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": event_id,
        "Host": target.host_header,
        "User-Agent": "TruthPoliticians-loopback-test/0.1",
        "Connection": "close",
        **_loopback_auth_headers(),
    }
    status, raw = _loopback_http_post(target, body=body, headers=headers, timeout=timeout, opener=opener)
    if status == 429:
        raise WordPressUnavailable("loopback_http_429")
    if status not in {200, 201, 202}:
        snippet = raw.decode("utf-8", errors="replace")[:400]
        raise WordPressUnavailable(f"loopback_http_{status}:{snippet}")
    try:
        parsed_body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WordPressUnavailable("loopback_response_invalid_json") from exc
    if not isinstance(parsed_body, dict):
        raise WordPressUnavailable("loopback_response_invalid")
    if os.environ.get("DV_WORDPRESS_LOOPBACK_TEST") == "1":
        parsed_body["_loopback_http_status"] = status
    return parsed_body
