"""Serveur HTTP strict loopback pour fautes RECETTE-10 (429/timeout réels sur socket).

Uniquement 127.0.0.1 ; aucune destination externe. Activé seulement en test/local
via DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR=1.
"""
from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

STRICT_ADDR = "127.0.0.1"
DEFAULT_FAULT_PORT = 12789
MAX_BODY_BYTES = 1024 * 1024

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_request_log: list[str] = []


def fault_server_port() -> int:
    raw = os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_SERVER_PORT", str(DEFAULT_FAULT_PORT)).strip()
    return int(raw) if raw.isdigit() else DEFAULT_FAULT_PORT


def fault_timeout_delay_seconds() -> float:
    raw = os.environ.get("DV_WORDPRESS_LOOPBACK_FAULT_TIMEOUT_DELAY", "25").strip()
    try:
        return float(raw)
    except ValueError:
        return 25.0


def pop_request_log() -> list[str]:
    return list(_request_log)


def clear_request_log() -> None:
    _request_log.clear()


class _FaultHandler(BaseHTTPRequestHandler):
    server_version = "DVLoopbackFaultInjector/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _reject(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        client = self.client_address[0]
        if client not in {STRICT_ADDR, "::1"}:
            self._reject(403, "forbidden_client")
            return
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY_BYTES:
            self._reject(413, "body_too_large")
            return
        if length:
            self.rfile.read(length)
        _request_log.append(f"POST {path} from={client}")
        if path == "/dv-loopback-fault/429":
            payload = b'{"code":"dv_import_rejected","message":"rate_limit_exceeded","data":{"status":429}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/dv-loopback-fault/timeout":
            time.sleep(fault_timeout_delay_seconds())
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._reject(404, "unknown_fault_path")

    def do_GET(self) -> None:  # noqa: N802
        self._reject(405, "method_not_allowed")


def ensure_fault_server_running() -> int:
    global _server
    with _server_lock:
        if _server is not None:
            return _server.server_address[1]
        port = fault_server_port()
        httpd = ThreadingHTTPServer((STRICT_ADDR, port), _FaultHandler)
        thread = threading.Thread(target=httpd.serve_forever, name="dv-loopback-fault", daemon=True)
        thread.start()
        _server = httpd
        return port


def shutdown_fault_server() -> None:
    global _server
    with _server_lock:
        if _server is None:
            return
        _server.shutdown()
        _server.server_close()
        _server = None


class fault_server_session:
    """Contexte test : démarre l'injecteur strict loopback et l'arrête à la sortie."""

    def __enter__(self) -> int:
        clear_request_log()
        return ensure_fault_server_running()

    def __exit__(self, *exc: object) -> None:
        shutdown_fault_server()
        clear_request_log()
