"""Client HTTP borné pour preuves RACC-001 — origine mission fixe, proxies explicitement désactivés."""
from __future__ import annotations

import io
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Mapping

MISSION_HTTP_ORIGIN = "http://127.0.0.1:18765"
MISSION_HOST = "127.0.0.1"
MISSION_PORT = 18765
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5

_ALLOWED_REDIRECT_PREFIXES = (
    "/connexion/",
    "/dossiers/",
    "/sante/",
)


def _redirect_path_allowed(path: str) -> bool:
    if path == "/":
        return True
    return any(path.startswith(prefix) for prefix in _ALLOWED_REDIRECT_PREFIXES)


def _private_headers() -> dict[str, str]:
    return {
        "X-DV-Private-Entry": "1",
        "X-Forwarded-Proto": "https",
    }


def _assert_mission_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        raise ValueError("userinfo_forbidden")
    if parsed.scheme != "http":
        raise ValueError(f"scheme_forbidden:{parsed.scheme}")
    host = parsed.hostname or ""
    if host != MISSION_HOST:
        raise ValueError(f"host_forbidden:{host}")
    port = parsed.port if parsed.port is not None else 80
    if port != MISSION_PORT:
        raise ValueError(f"port_forbidden:{port}")
    return parsed


def _mission_origin_from_base(base_url: str) -> str:
    parsed = _assert_mission_url(base_url.rstrip("/") + "/")
    return f"http://{parsed.hostname}:{MISSION_PORT}"


class _MissionHTTPHandler(urllib.request.HTTPHandler):
    mission_proxy_neutralized = True


def _build_mission_opener(
    jar: CookieJar,
    redirect_handler: urllib.request.BaseHandler,
) -> urllib.request.OpenerDirector:
    # OpenerDirector manuel : build_opener() injecte toujours ProxyHandler(getproxies()).
    opener = urllib.request.OpenerDirector()
    opener.add_handler(_MissionHTTPHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(urllib.request.HTTPCookieProcessor(jar))
    opener.add_handler(redirect_handler)
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


def _read_bounded(resp, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = resp.read(65536)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ValueError("response_too_large")
        chunks.append(block)
    return b"".join(chunks)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _MissionRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = origin.rstrip("/")
        self._redirects = 0

    def reset_redirect_chain(self) -> None:
        """Réinitialise le compteur pour une nouvelle requête HTTP (pas cumul inter-requêtes)."""
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirects += 1
        if self._redirects > MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "redirect_limit_exceeded",
                headers,
                fp,
            )
        target = urllib.parse.urlparse(newurl)
        if target.username or target.password:
            raise urllib.error.HTTPError(req.full_url, code, "redirect_userinfo_forbidden", headers, fp)
        if target.scheme != "http" or target.hostname != MISSION_HOST or (target.port or 80) != MISSION_PORT:
            raise urllib.error.HTTPError(req.full_url, code, "cross_origin_redirect_forbidden", headers, fp)
        path = target.path or "/"
        if not _redirect_path_allowed(path):
            raise urllib.error.HTTPError(req.full_url, code, f"redirect_path_forbidden:{path}", headers, fp)
        if code in {301, 302, 303}:
            method = "GET"
            data = None
        elif code in {307, 308}:
            method = req.get_method()
            data = req.data
        else:
            raise urllib.error.HTTPError(req.full_url, code, f"redirect_code_forbidden:{code}", headers, fp)
        merged = dict(req.headers)
        merged.update(_private_headers())
        return urllib.request.Request(
            newurl,
            data=data,
            headers=merged,
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method=method,
        )


def login_page_visible(body: str) -> bool:
    return 'name="username"' in body and "csrfmiddlewaretoken" in body


def session_authenticated(body: str) -> bool:
    if login_page_visible(body):
        return False
    return "Déconnexion" in body or "deconnexion" in body.lower() or "/deconnexion/" in body


class Racc001LoopbackHttpClient:
    def __init__(self, base_url: str | None = None) -> None:
        origin = _mission_origin_from_base(base_url or MISSION_HTTP_ORIGIN)
        if origin != MISSION_HTTP_ORIGIN:
            raise ValueError("mission_origin_mismatch")
        self.base = MISSION_HTTP_ORIGIN
        self.jar = CookieJar()
        self._login_opener = _build_mission_opener(self.jar, _NoRedirectHandler())
        self._redirect_handler = _MissionRedirectHandler(self.base)
        self.opener = _build_mission_opener(self.jar, self._redirect_handler)

    def _begin_http_request(self) -> None:
        self._redirect_handler.reset_redirect_chain()

    @staticmethod
    def opener_uses_empty_proxy_handler(opener: urllib.request.OpenerDirector) -> bool:
        candidates = list(getattr(opener, "handlers", []))
        if hasattr(opener, "handle_open"):
            candidates.extend(getattr(opener, "handle_open", {}).values())
        has_proxy = any(isinstance(handler, urllib.request.ProxyHandler) for handler in candidates)
        has_direct = any(getattr(handler, "mission_proxy_neutralized", False) for handler in candidates)
        return has_direct and not has_proxy

    def request(
        self,
        method: str,
        path: str,
        data: Mapping[str, str] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        referer_path: str | None = None,
    ) -> tuple[int, str]:
        if path.startswith("http://"):
            _assert_mission_url(path)
            url = path
        else:
            if not path.startswith("/"):
                raise ValueError("path_must_be_absolute")
            url = self.base + path
        _assert_mission_url(url)
        hdrs = dict(_private_headers())
        if referer_path is not None:
            secure_base = self.base.replace("http://", "https://", 1)
            hdrs["Referer"] = secure_base + referer_path
        if extra_headers:
            hdrs.update(extra_headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        self._begin_http_request()
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = _read_bounded(resp)
                return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = _read_bounded(exc)
            return exc.code, raw.decode("utf-8", errors="replace")

    def csrf_from_page(self, path: str) -> str:
        status, html = self.request("GET", path)
        if status != 200:
            raise RuntimeError(f"csrf_page_failed path={path} status={status}")
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
        if not match:
            raise RuntimeError(f"csrf_token_missing path={path}")
        return match.group(1)

    def login(self, username: str, password: str) -> int:
        csrf = self.csrf_from_page("/connexion/")
        url = self.base + "/connexion/"
        _assert_mission_url(url)
        body = urllib.parse.urlencode(
            {
                "csrfmiddlewaretoken": csrf,
                "username": username,
                "password": password,
                "next": "/",
            }
        ).encode("utf-8")
        hdrs = dict(_private_headers())
        secure_base = self.base.replace("http://", "https://", 1)
        hdrs["Referer"] = secure_base + "/connexion/"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
        try:
            with self._login_opener.open(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def login_outcome(self, username: str, password: str) -> tuple[int, bool]:
        """Retourne (code HTTP login, session_authentifiée_après_login)."""
        code = self.login(username, password)
        if code in {302, 303}:
            status, body = self.request("GET", "/")
            return code, session_authenticated(body)
        if code == 200:
            _, body = self.request("GET", "/connexion/")
            return code, False
        return code, False

    def post_multipart(
        self,
        path: str,
        fields: Mapping[str, str],
        file_field: str,
        file_name: str,
        file_bytes: bytes,
        content_type: str = "application/json",
    ) -> tuple[int, str]:
        url = self.base + path
        _assert_mission_url(url)
        if len(file_bytes) > MAX_RESPONSE_BYTES:
            raise ValueError("upload_too_large")
        boundary = "----Racc001Boundary7MA4YWxk"
        body = io.BytesIO()
        for key, value in fields.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.write(f"{value}\r\n".encode())
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode()
        )
        body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.write(file_bytes)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        payload = body.getvalue()
        hdrs = dict(_private_headers())
        hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        hdrs["Content-Length"] = str(len(payload))
        req = urllib.request.Request(url, data=payload, method="POST", headers=hdrs)
        self._begin_http_request()
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = _read_bounded(resp)
                return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = _read_bounded(exc)
            return exc.code, raw.decode("utf-8", errors="replace")


# Compatibilité import historique des commandes de preuve.
_assert_loopback_url = _assert_mission_url
