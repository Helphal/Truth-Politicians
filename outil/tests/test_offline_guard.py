"""The test guard permits only explicit literal loopback, never an outside target."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class OfflineGuardTests(unittest.TestCase):
    def run_guarded(self, source, allow_loopback):
        env = os.environ.copy()
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts/offline_guard'),
                   DV_AUDIT_ALLOW_LOOPBACK='1' if allow_loopback else '0')
        result = subprocess.run([sys.executable, '-c', source], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_external_dns_and_numeric_connection_refused_even_with_loopback(self):
        self.run_guarded("""
import socket
actions = [lambda: socket.getaddrinfo('example.org', 443),
           lambda: socket.getaddrinfo('192.0.2.1', 443),
           lambda: socket.socket().connect(('192.0.2.1', 443)),
           lambda: socket.socket().bind(('0.0.0.0', 0))]
for action in actions:
    try: action()
    except RuntimeError as e: assert str(e).startswith('AUDIT_REAL_NETWORK_DISABLED:')
    else: raise AssertionError('external operation escaped guard')
""", True)

    def test_loopback_still_refused_without_opt_in(self):
        self.run_guarded("""
import socket
try: socket.socket().connect(('127.0.0.1', 1))
except RuntimeError as e: assert str(e) == 'AUDIT_REAL_NETWORK_DISABLED:socket.connect'
else: raise AssertionError('loopback should be disabled')
""", False)

    def test_explicit_literal_loopback_can_exchange_bytes(self):
        self.run_guarded("""
import socket, threading
with socket.socket() as server:
    server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(3)
    def serve():
        conn, _ = server.accept()
        with conn: conn.sendall(b'SYNTH-local-only')
    thread = threading.Thread(target=serve); thread.start()
    with socket.create_connection(server.getsockname(), timeout=3) as client:
        assert client.recv(30) == b'SYNTH-local-only'
    thread.join(3); assert not thread.is_alive()
""", True)
