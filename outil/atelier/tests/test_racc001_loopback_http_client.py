"""Tests hors réseau du client HTTP mission RACC-001."""
from __future__ import annotations

import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from unittest import TestCase
from unittest.mock import patch

from atelier.services.racc001_loopback_http_client import (
    MAX_REDIRECTS,
    MISSION_HTTP_ORIGIN,
    Racc001LoopbackHttpClient,
    _MissionRedirectHandler,
    _assert_mission_url,
    _build_mission_opener,
    _redirect_path_allowed,
    login_page_visible,
)


class MissionUrlTests(TestCase):
    def test_accepts_mission_origin_only(self):
        _assert_mission_url(MISSION_HTTP_ORIGIN + "/sante/")

    def test_rejects_localhost_dns(self):
        with self.assertRaises(ValueError):
            _assert_mission_url("http://localhost:18765/sante/")

    def test_rejects_wrong_port(self):
        with self.assertRaises(ValueError):
            _assert_mission_url("http://127.0.0.1:9999/sante/")

    def test_rejects_userinfo(self):
        with self.assertRaises(ValueError):
            _assert_mission_url("http://user:pass@127.0.0.1:18765/")

    def test_client_rejects_non_mission_base(self):
        with self.assertRaises(ValueError):
            Racc001LoopbackHttpClient("http://127.0.0.1:9999")


class ProxyNeutralizationTests(TestCase):
    def test_opener_installs_empty_proxy_handler(self):
        jar = CookieJar()
        opener = _build_mission_opener(jar, urllib.request.HTTPRedirectHandler())
        self.assertTrue(Racc001LoopbackHttpClient.opener_uses_empty_proxy_handler(opener))

    @patch(
        "urllib.request.getproxies",
        return_value={"http": "http://proxy.evil:8080", "https": "http://proxy.evil:8080"},
    )
    def test_proxy_env_does_not_replace_empty_proxy_handler(self, _mock_getproxies):
        client = Racc001LoopbackHttpClient()
        for opener in (client.opener, client._login_opener):
            names = [type(handler).__name__ for handler in opener.handlers]
            self.assertIn("_MissionHTTPHandler", names)
            self.assertNotIn("ProxyHandler", names)

    def test_login_opener_also_neutralizes_proxies(self):
        client = Racc001LoopbackHttpClient()
        self.assertTrue(Racc001LoopbackHttpClient.opener_uses_empty_proxy_handler(client._login_opener))
        self.assertTrue(Racc001LoopbackHttpClient.opener_uses_empty_proxy_handler(client.opener))


class RedirectSemanticsTests(TestCase):
    def test_302_drops_post_body(self):
        handler = _MissionRedirectHandler(MISSION_HTTP_ORIGIN)
        req = urllib.request.Request(
            MISSION_HTTP_ORIGIN + "/connexion/",
            data=b"post-body",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        redirected = handler.redirect_request(
            req,
            None,
            302,
            "Found",
            {},
            MISSION_HTTP_ORIGIN + "/dossiers/1/",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_method(), "GET")
        self.assertIsNone(redirected.data)

    def test_cross_port_redirect_forbidden(self):
        handler = _MissionRedirectHandler(MISSION_HTTP_ORIGIN)
        req = urllib.request.Request(MISSION_HTTP_ORIGIN + "/connexion/", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                req,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1:9999/dossiers/1/",
            )
        self.assertIn("cross_origin", str(ctx.exception))

    def test_redirect_path_root_exact_only(self):
        self.assertTrue(_redirect_path_allowed("/"))
        self.assertTrue(_redirect_path_allowed("/connexion/"))
        self.assertTrue(_redirect_path_allowed("/dossiers/12/"))
        self.assertFalse(_redirect_path_allowed("/admin/"))
        self.assertFalse(_redirect_path_allowed("/connexion-extra/"))

    def test_six_redirect_hops_in_one_chain_refused(self):
        handler = _MissionRedirectHandler(MISSION_HTTP_ORIGIN)
        req = urllib.request.Request(MISSION_HTTP_ORIGIN + "/connexion/", method="GET")
        target = MISSION_HTTP_ORIGIN + "/dossiers/1/"
        for _ in range(MAX_REDIRECTS):
            redirected = handler.redirect_request(req, None, 302, "Found", {}, target)
            self.assertIsNotNone(redirected)
            req = redirected
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, target)
        self.assertIn("redirect_limit", str(ctx.exception))

    def test_six_independent_single_hop_requests_ok(self):
        handler = _MissionRedirectHandler(MISSION_HTTP_ORIGIN)
        for _ in range(6):
            handler.reset_redirect_chain()
            req = urllib.request.Request(MISSION_HTTP_ORIGIN + "/connexion/", method="GET")
            redirected = handler.redirect_request(
                req,
                None,
                302,
                "Found",
                {},
                MISSION_HTTP_ORIGIN + "/dossiers/1/",
            )
            self.assertIsNotNone(redirected)
            self.assertEqual(handler._redirects, 1)

    def test_client_resets_redirect_counter_per_request(self):
        client = Racc001LoopbackHttpClient()
        handler = client._redirect_handler
        with patch.object(handler, "reset_redirect_chain", wraps=handler.reset_redirect_chain) as spy:
            with patch.object(client.opener, "open") as mock_open:
                mock_resp = mock_open.return_value.__enter__.return_value
                mock_resp.status = 200
                mock_resp.read = lambda *a, **k: b""
                for _ in range(6):
                    client.request("GET", "/sante/")
            self.assertEqual(spy.call_count, 6)


class LoginPageHeuristicsTests(TestCase):
    def test_login_page_detection(self):
        self.assertTrue(login_page_visible('<input name="username"><input name="csrfmiddlewaretoken">'))
        self.assertFalse(login_page_visible("<a href='/deconnexion/'>Déconnexion</a>"))
