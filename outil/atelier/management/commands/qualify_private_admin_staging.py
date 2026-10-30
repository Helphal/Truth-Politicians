"""Qualification PRIVATE-ADMIN-1 via Caddy staging HTTPS (comptes synthétiques uniquement)."""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import urllib.parse
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, SystemCheckError

from atelier.models import UserProfile
from atelier.services.accounts import create_account

STAGING_MARKER_FILE = ".dv-staging-volume-marker"
STAGING_MARKER_VALUE = "dv-build003-staging-only"


class Command(BaseCommand):
    help = (
        "Qualifie la frontière admin sur stack staging HTTPS (Caddy + certificat de test). "
        "Fail-closed hors volume marqué ; comptes synthétiques en mémoire uniquement."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--report",
            default="",
            help="Rapport JSON sous runtime (défaut: staging-private-admin-qualification.json)",
        )

    def handle(self, *args, **options):
        self._require_marked_staging_volume()
        public_base = os.environ.get("STAGING_PUBLIC_URL", "").rstrip("/")
        lan_base = os.environ.get("STAGING_LAN_URL", "").rstrip("/")
        ca_file = os.environ.get("STAGING_TLS_CA_FILE", "")
        if not public_base or not lan_base or not ca_file:
            raise CommandError("staging_https_config_missing")
        if not public_base.startswith("https://") or not lan_base.startswith("https://"):
            raise CommandError("staging_urls_must_be_https")

        report_path = Path(
            options["report"] or settings.RUNTIME_DIR / "staging-private-admin-qualification.json"
        )
        created_user_ids: list[int] = []
        results: dict[str, object] = {
            "marker": "DV-NAS-BUILD003-PRIVATE-ADMIN-STAGING",
            "public_entry_scheme": "https",
            "lan_entry_scheme": "https",
            "tls_verify": "staging_ca_file",
            "checks": {},
            "note": "Entrée LAN sur interface privée réelle (:28797). Public loopback (:28795) hors atteinte LAN.",
        }

        try:
            deploy_output = self._capture_check_deploy()
            results["checks"]["manage_check_deploy"] = deploy_output
            results["django_private_entry"] = self._django_private_entry_snapshot()

            public = _HttpsStagingSession(
                public_base,
                ca_file=ca_file,
                connect_host=os.environ.get("STAGING_PUBLIC_CONNECT_HOST", "127.0.0.1"),
            )
            lan = _HttpsStagingSession(
                lan_base,
                ca_file=ca_file,
                connect_host=os.environ.get("STAGING_LAN_CONNECT_HOST", ""),
            )
            if not lan.connect_host:
                raise CommandError("staging_lan_connect_host_missing")

            results["checks"]["proxy_signal_lan_comptes_unauth"] = lan.get_status("/comptes/")
            results["checks"]["proxy_signal_public_comptes_unauth"] = public.get_status("/comptes/")
            results["checks"]["proxy_signal_public_comptes_forged_header"] = public.get_status(
                "/comptes/", extra_headers={"X-DV-Private-Entry": "1"}
            )

            admin_login, admin_password, editor_login, editor_password, created_user_ids = (
                self._create_synthetic_accounts()
            )
            results["checks"]["synthetic_accounts_seeded"] = True

            public_editor = _HttpsStagingSession(
                public_base,
                ca_file=ca_file,
                connect_host=os.environ.get("STAGING_PUBLIC_CONNECT_HOST", "127.0.0.1"),
            )

            results["checks"]["editor_login_public"] = public_editor.login(
                editor_login, editor_password
            )
            results["checks"]["editor_dashboard_public"] = public_editor.get_status("/")
            results["checks"]["editor_accounts_public"] = public_editor.get_status("/comptes/")

            results["checks"]["admin_login_public"] = public.login(admin_login, admin_password)
            results["checks"]["admin_login_public_forged_header"] = public.login(
                admin_login, admin_password, extra_headers={"X-DV-Private-Entry": "1"}
            )
            results["checks"]["admin_login_lan"] = lan.login(admin_login, admin_password)
            results["checks"]["admin_accounts_list_lan"] = lan.get_status("/comptes/")
            results["checks"]["admin_accounts_list_public"] = public.get_status("/comptes/")

            lan.login(admin_login, admin_password)
            results["checks"]["admin_dashboard_lan_after_login"] = lan.get_status("/")
            public.import_cookies(lan.export_cookies())
            results["checks"]["admin_dashboard_public_with_session"] = public.get_status("/")

            expected = {
                "proxy_signal_lan_comptes_unauth": 302,
                "proxy_signal_public_comptes_unauth": 403,
                "proxy_signal_public_comptes_forged_header": 403,
                "admin_login_public": 403,
                "admin_login_public_forged_header": 403,
                "admin_login_lan": 302,
                "admin_accounts_list_lan": 200,
                "admin_accounts_list_public": 403,
                "admin_dashboard_lan_after_login": 200,
                "admin_dashboard_public_with_session": 403,
                "editor_login_public": 302,
                "editor_dashboard_public": 200,
                "editor_accounts_public": 403,
            }
            failures = [
                {"check": key, "expected": want, "got": results["checks"].get(key)}
                for key, want in expected.items()
                if results["checks"].get(key) != want
            ]
            results["expected"] = expected
            results["failures"] = failures
            results["status"] = "pass" if not failures and deploy_output.get("ok") else "fail"

            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            if failures or not deploy_output.get("ok"):
                raise CommandError(f"staging_qualification_failed failures={len(failures)}")
            self.stdout.write(self.style.SUCCESS(f"status=pass report={report_path.name}"))
        finally:
            self._cleanup_created_accounts(created_user_ids)

    def _require_marked_staging_volume(self) -> None:
        runtime = settings.RUNTIME_DIR.resolve()
        marker = runtime / STAGING_MARKER_FILE
        if not marker.is_file():
            raise CommandError("staging_volume_unmarked")
        if marker.read_text(encoding="utf-8").strip() != STAGING_MARKER_VALUE:
            raise CommandError("staging_volume_marker_invalid")
        if os.environ.get("DV_STAGING_QUALIFICATION") != "1":
            raise CommandError("staging_qualification_env_not_set")
        db_path = Path(str(settings.DATABASES["default"]["NAME"])).resolve()
        if db_path.parent.resolve() != runtime:
            raise CommandError("staging_database_outside_runtime")
        ca_path = Path(os.environ.get("STAGING_TLS_CA_FILE", "")).resolve()
        if not ca_path.is_file():
            raise CommandError("staging_tls_ca_missing")

    def _django_private_entry_snapshot(self) -> dict[str, object]:
        return {
            "dv_env": settings.DV_ENV,
            "private_entry_mode": getattr(settings, "DV_PRIVATE_ENTRY_MODE", ""),
            "trust_private_entry_proxy": bool(
                getattr(settings, "DV_TRUST_PRIVATE_ENTRY_PROXY", False)
            ),
            "private_entry_header_meta": getattr(
                settings, "DV_PRIVATE_ENTRY_HEADER_META", ""
            ),
            "trust_proxy_ssl_header": bool(
                os.environ.get("DV_TRUST_PROXY_SSL_HEADER", "0") == "1"
            ),
            "csrf_trusted_origins_count": len(getattr(settings, "CSRF_TRUSTED_ORIGINS", [])),
        }

    def _capture_check_deploy(self) -> dict[str, object]:
        from io import StringIO

        buffer = StringIO()
        try:
            call_command("check", "--deploy", stdout=buffer)
            text = buffer.getvalue()
            return {"ok": True, "warnings": text.count("security.W"), "errors": 0}
        except SystemCheckError as exc:
            return {"ok": False, "detail": str(exc).splitlines()[:5]}

    def _create_synthetic_accounts(self) -> tuple[str, str, str, str, list[int]]:
        user_model = get_user_model()
        created: list[int] = []
        try:
            admin_creds = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
            created.append(admin_creds.user_id)
            admin_user = user_model.objects.get(pk=admin_creds.user_id)
            admin_user.dv_profile.must_change_password = False
            admin_user.dv_profile.save()
            admin_password = admin_creds.temporary_password or ""

            editor_creds = create_account(
                role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True
            )
            created.append(editor_creds.user_id)
            editor_user = user_model.objects.get(pk=editor_creds.user_id)
            editor_user.dv_profile.must_change_password = False
            editor_user.dv_profile.save()
            editor_password = editor_creds.temporary_password or ""
        except Exception:
            self._cleanup_created_accounts(created)
            raise

        return (
            admin_creds.login_pseudonym,
            admin_password,
            editor_creds.login_pseudonym,
            editor_password,
            created,
        )

    def _cleanup_created_accounts(self, user_ids: list[int]) -> None:
        if not user_ids:
            return
        get_user_model().objects.filter(pk__in=user_ids, dv_profile__is_test_identity=True).delete()


class _HttpsStagingSession:
    def __init__(self, base_url: str, *, ca_file: str, connect_host: str):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CommandError("staging_url_invalid")
        self.base_url = base_url
        self.host_header = parsed.hostname
        self.port = parsed.port or 443
        self.connect_host = connect_host
        self.jar = CookieJar()
        self._ctx = ssl.create_default_context(cafile=ca_file)

    def _https_connection(self) -> http.client.HTTPConnection:
        sock = socket.create_connection((self.connect_host, self.port), timeout=60)
        conn = http.client.HTTPConnection(self.host_header, self.port)
        conn.sock = self._ctx.wrap_socket(sock, server_hostname=self.host_header)
        return conn

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirect: bool = False,
    ) -> int:
        conn = self._https_connection()
        merged = {"Host": self.host_header, "User-Agent": "DV-Staging-Qualify/1.0"}
        if headers:
            merged.update(headers)
        if body is not None:
            merged.setdefault("Content-Type", "application/x-www-form-urlencoded")
        cookie_header = self._cookie_header()
        if cookie_header:
            merged["Cookie"] = cookie_header
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        self._store_cookies(response)
        if follow_redirect and response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location", "")
            conn.close()
            if location.startswith("/"):
                return self._request("GET", location, follow_redirect=False)
            return response.status
        conn.close()
        return response.status

    def _cookie_header(self) -> str:
        parts = []
        for cookie in self.jar:
            parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)

    def _store_cookies(self, response: http.client.HTTPResponse) -> None:
        from http.cookies import SimpleCookie

        raw = "\n".join(v for k, v in response.getheaders() if k.lower() == "set-cookie")
        if not raw:
            return
        jar_cookie = SimpleCookie()
        jar_cookie.load(raw)
        for morsel in jar_cookie.values():
            self.jar.set_cookie(
                Cookie(
                    version=0,
                    name=morsel.key,
                    value=morsel.value,
                    port=None,
                    port_specified=False,
                    domain=morsel["domain"] or self.host_header,
                    domain_specified=bool(morsel["domain"]),
                    domain_initial_dot=False,
                    path=morsel["path"] or "/",
                    path_specified=True,
                    secure=bool(morsel["secure"]),
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

    def import_cookies(self, cookies: list[dict]) -> None:
        for item in cookies:
            self.jar.set_cookie(
                Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=item.get("domain", self.host_header),
                    domain_specified=True,
                    domain_initial_dot=False,
                    path=item.get("path", "/"),
                    path_specified=True,
                    secure=item.get("secure", True),
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

    def export_cookies(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
            }
            for c in self.jar
        ]

    def _csrf(self) -> str:
        status = self._request("GET", "/connexion/")
        if status != 200:
            raise CommandError(f"staging_csrf_get_failed status={status}")
        # Re-fetch body by new request - need body in _request
        conn = self._https_connection()
        headers = {"Host": self.host_header, "User-Agent": "DV-Staging-Qualify/1.0"}
        if self._cookie_header():
            headers["Cookie"] = self._cookie_header()
        conn.request("GET", "/connexion/", headers=headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        self._store_cookies(response)
        conn.close()
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
        if not match:
            raise CommandError("csrf_token_missing")
        return match.group(1)

    def login(self, username: str, password: str, *, extra_headers: dict[str, str] | None = None) -> int:
        token = self._csrf()
        data = urllib.parse.urlencode(
            {"username": username, "password": password, "csrfmiddlewaretoken": token}
        ).encode("utf-8")
        headers = {
            "Referer": f"{self.base_url}/connexion/",
            **(extra_headers or {}),
        }
        return self._request("POST", "/connexion/", body=data, headers=headers)

    def get_status(self, path: str, *, extra_headers: dict[str, str] | None = None) -> int:
        return self._request("GET", path, headers=extra_headers)
