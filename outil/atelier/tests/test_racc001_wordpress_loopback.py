"""R1-B — garde-fous loopback sans requête réseau réelle."""
import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from atelier.services.wordpress import WordPressUnavailable, _post_packet
from atelier.services.wordpress_loopback import (
    loopback_transport_enabled,
    post_packet_loopback,
    resolve_loopback_endpoint,
    validate_loopback_packet,
)


def _synthetic_packet() -> dict:
    return {
        "event_id": "00000000-0000-4000-8000-000000000001",
        "environment": "test",
        "destination": "dv-site:test",
        "payload": {"data_scope": "synthetic", "public": {}, "content_kind": "speech_vote"},
    }


class WordPressLoopbackPolicyTests(SimpleTestCase):
    @override_settings(DV_ENV="test")
    def test_loopback_disabled_by_default(self):
        self.assertFalse(loopback_transport_enabled())

    @override_settings(DV_ENV="production")
    def test_loopback_never_in_production(self):
        with patch.dict(os.environ, {"DV_WORDPRESS_LOOPBACK_TEST": "1"}, clear=False):
            self.assertFalse(loopback_transport_enabled())

    def test_resolve_rejects_arbitrary_local_suffix_without_network(self):
        for endpoint in (
            "http://printer.local/wp-json/dv/v1/event",
            "http://nas.local/",
            "http://evil.localhost.attacker.example/",
        ):
            with self.assertRaises(WordPressUnavailable):
                resolve_loopback_endpoint(endpoint)

    def test_resolve_accepts_literal_loopback_and_local_b_virtual_host(self):
        t1 = resolve_loopback_endpoint("http://127.0.0.1/wp-json/dv/v1/event")
        self.assertEqual(t1.connect_addr, "127.0.0.1")
        t2 = resolve_loopback_endpoint(
            "http://truth-politicians.test/wp-json/dv/v1/event"
        )
        self.assertEqual(t2.connect_addr, "127.0.0.1")
        self.assertEqual(t2.host_header, "truth-politicians.test")

    def test_resolve_includes_nonstandard_port_in_host_header(self):
        target = resolve_loopback_endpoint("http://127.0.0.1:10040/comparer/")
        self.assertEqual(target.port, 10040)
        self.assertEqual(target.host_header, "truth-politicians.test:10040")

    def test_resolve_rejects_userinfo_and_public_host(self):
        with self.assertRaises(WordPressUnavailable):
            resolve_loopback_endpoint("http://user:pass@127.0.0.1/")
        with self.assertRaises(WordPressUnavailable):
            resolve_loopback_endpoint("https://example.org/wp-json/dv/v1/event")

    def test_packet_guards_environment_destination_scope(self):
        base = _synthetic_packet()
        validate_loopback_packet(base)
        bad = dict(base, environment="production")
        with self.assertRaises(WordPressUnavailable):
            validate_loopback_packet(bad)
        bad = dict(base, destination="dv-site:production")
        with self.assertRaises(WordPressUnavailable):
            validate_loopback_packet(bad)
        bad = dict(base, payload={"data_scope": "real", "public": {}})
        with self.assertRaises(WordPressUnavailable):
            validate_loopback_packet(bad)

    def test_post_packet_requires_event_id(self):
        with self.assertRaises(WordPressUnavailable):
            _post_packet({"environment": "test"}, timeout=1)


class WordPressLoopbackHttpTests(SimpleTestCase):
    def _env(self):
        return patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_LOOPBACK_TEST": "1",
                "DV_WORDPRESS_LOOPBACK_ENDPOINT": "http://127.0.0.1/wp-json/dv/v1/event",
            },
            clear=False,
        )

    @override_settings(DV_ENV="test")
    def test_single_connection_to_literal_loopback_no_redirect(self):
        conn = MagicMock()
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok":true}'
        conn.getresponse.return_value = response

        def opener(host, port, timeout=20):
            self.assertEqual(host, "127.0.0.1")
            return conn

        with self._env():
            body = post_packet_loopback(_synthetic_packet(), opener=opener)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("_loopback_http_status"), 200)
        conn.request.assert_called_once()
        self.assertEqual(conn.request.call_args[0][0], "POST")
        headers = conn.request.call_args[1]["headers"]
        self.assertEqual(headers["Host"], "truth-politicians.test")
        self.assertEqual(headers["Connection"], "close")
        conn.close.assert_called_once()

    @override_settings(DV_ENV="test")
    def test_http_429_and_invalid_json_bounded(self):
        conn = MagicMock()
        response = MagicMock()
        response.status = 429
        response.read.return_value = b""
        conn.getresponse.return_value = response

        with self._env():
            with self.assertRaises(WordPressUnavailable) as ctx:
                post_packet_loopback(_synthetic_packet(), opener=lambda *a, **k: conn)
            self.assertIn("429", str(ctx.exception))

        response.status = 200
        response.read.return_value = b"not-json"
        with self._env():
            with self.assertRaises(WordPressUnavailable):
                post_packet_loopback(_synthetic_packet(), opener=lambda *a, **k: conn)

    @override_settings(DV_ENV="test")
    def test_loopback_rejects_public_host_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_LOOPBACK_TEST": "1",
                "DV_WORDPRESS_LOOPBACK_ENDPOINT": "https://example.org/wp-json/dv/v1/event",
            },
            clear=False,
        ):
            with self.assertRaises(WordPressUnavailable):
                post_packet_loopback(_synthetic_packet())
