"""Audit de comptes — schéma fermé, sans IP / UA / empreinte (BUILD-003 R2).

Frontière de persistance ``account.*`` uniquement : seules des métadonnées
autorisées (types et valeurs contrôlés, motifs génériques) sont écrites.
Les valeurs rejetées ne sont jamais recopiées dans un autre champ ni journalisées.
Les journaux proxy NAS restent hors périmètre. Ne jamais journaliser ``request.META``.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any

from atelier.models import AuditEvent

# --- Schéma fermé pour AuditEvent.detail des événements account.* ---
# Clés et types autorisés ; toute autre clé / valeur est abandonnée sans copie.
_ALLOWED_ACCESS_MODES = frozenset({"invite", "temp_password"})
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Schéma : clé → validateur renvoyant la valeur normalisée ou None (rejet).
def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 1_000_000:
        return value
    return None


def _as_optional_user_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _as_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _REASON_RE.match(candidate):
        return None
    return candidate


def _as_role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if not _ROLE_RE.match(candidate):
        return None
    return candidate


def _as_access_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate not in _ALLOWED_ACCESS_MODES:
        return None
    return candidate


def _as_action(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _REASON_RE.match(candidate):
        return None
    return candidate


_CLOSED_SCHEMA: dict[str, Any] = {
    "action": _as_action,
    "success": _as_bool,
    "reason": _as_reason,
    "target_user_id": _as_optional_user_id,
    "session_revoked": _as_bool,
    "role": _as_role,
    "access_mode": _as_access_mode,
    "previous_length": _as_nonneg_int,
    "new_length": _as_nonneg_int,
    "from": _as_role,
    "to": _as_role,
    "sessions_deleted": _as_nonneg_int,
    "session_epoch": _as_nonneg_int,
    "private_entry": _as_bool,
}


def build_closed_account_detail(raw: dict | None) -> dict[str, Any]:
    """Construit un détail conforme au schéma fermé ; ignore toute clé/valeur non autorisée."""
    if not isinstance(raw, dict):
        return {}
    closed: dict[str, Any] = {}
    for key, validator in _CLOSED_SCHEMA.items():
        if key not in raw:
            continue
        # Clé IP / surveillance : le schéma ne les contient pas ; pas de copie.
        normalized = validator(raw[key])
        if normalized is None and key == "target_user_id" and raw[key] is None:
            closed[key] = None
            continue
        if normalized is None:
            continue
        closed[key] = normalized
    return closed


# --- Détection défensive (sanitize) : hors schéma fermé, pour tests / utilitaires ---
_FORBIDDEN_SURVEILLANCE_KEYS = frozenset(
    {
        "ip",
        "ip_address",
        "remote_addr",
        "remote_address",
        "x_forwarded_for",
        "x-forwarded-for",
        "forwarded",
        "client_ip",
        "user_agent",
        "ua",
        "http_user_agent",
        "fingerprint",
        "device_fingerprint",
        "browser_fingerprint",
        "canvas_fingerprint",
        "meta",
        "request_meta",
        "environ",
        "description",
        "summary",
        "note",
        "message",
        "text",
        "comment",
    }
)

_IPV4_RE = re.compile(
    r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
)
_IPV6_CANDIDATE_RE = re.compile(
    r"(?:\[[0-9a-fA-F:]+\](?::\d+)?)|(?:(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})"
)
_UA_HINT_RE = re.compile(
    r"(?i)\b(?:mozilla|chrome|safari|firefox|curl|wget|user-agent|applewebkit)\b"
)


def _extract_bracket_host(value: str) -> str | None:
    """Extrait l'hôte d'une forme ``[IPv6]:port`` ou ``[IPv6]``."""
    if not value.startswith("["):
        return None
    end = value.find("]")
    if end <= 1:
        return None
    return value[1:end]


def _is_network_literal(value: str) -> bool:
    """Détecte un littéral IPv4/IPv6 (y compris ``[v6]:port``) ou une valeur réseau évidente."""
    s = value.strip()
    if not s:
        return False
    lowered = s.casefold()
    if "user-agent" in lowered or "x-forwarded-for" in lowered:
        return True
    if lowered in {"localhost", "::1", "0.0.0.0", "127.0.0.1", "remote_addr"}:
        return True

    bracket_host = _extract_bracket_host(s)
    if bracket_host is not None:
        try:
            ipaddress.ip_address(bracket_host)
            return True
        except ValueError:
            pass

    candidate = s
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass

    # host:port IPv4
    if candidate.count(":") == 1:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                if _IPV4_RE.fullmatch(host):
                    return True
    return False


def _contains_network_or_ua_material(value: str) -> bool:
    """True si le texte libre embarque une IP, un hint UA ou un littéral réseau."""
    if _is_network_literal(value):
        return True
    if _IPV4_RE.search(value):
        return True
    if _UA_HINT_RE.search(value):
        return True
    for match in _IPV6_CANDIDATE_RE.finditer(value):
        token = match.group(0)
        host = _extract_bracket_host(token) or token
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            continue
    return False


def _key_looks_like_meta(key_l: str) -> bool:
    if key_l in _FORBIDDEN_SURVEILLANCE_KEYS:
        return True
    if _is_network_literal(key_l) or _IPV4_RE.search(key_l):
        return True
    if key_l.startswith("http_") or key_l.startswith("remote_"):
        return True
    if any(
        token in key_l
        for token in ("fingerprint", "user_agent", "remote_addr", "forwarded", "client_ip")
    ):
        return True
    return False


def sanitize_audit_detail(detail: dict | list | tuple | None) -> dict | list:
    """Filtre récursif défensif (clés IP, littéraux, texte libre réseau/UA).

    La persistance ``account.*`` passe par ``build_closed_account_detail`` ;
    ce filtre reste disponible pour les tests et contrôles défensifs.
    """
    if detail is None:
        return {}
    if isinstance(detail, tuple):
        detail = list(detail)
    if isinstance(detail, list):
        cleaned_list: list[Any] = []
        for item in detail:
            if isinstance(item, (dict, list, tuple)):
                nested = sanitize_audit_detail(item)
                if nested or nested == []:
                    cleaned_list.append(nested)
                continue
            if isinstance(item, str) and _contains_network_or_ua_material(item):
                continue
            cleaned_list.append(item)
        return cleaned_list
    if not isinstance(detail, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        key_l = str(key).casefold().replace("-", "_")
        if _key_looks_like_meta(key_l):
            continue
        if isinstance(value, (dict, list, tuple)):
            nested = sanitize_audit_detail(value)
            if nested or nested == []:
                cleaned[str(key)] = nested
            continue
        if isinstance(value, str):
            if _contains_network_or_ua_material(value):
                continue
        cleaned[str(key)] = value
    return cleaned


def record_account_audit(
    *,
    actor,
    action: str,
    target_user,
    success: bool,
    reason: str = "",
    detail: dict | None = None,
) -> AuditEvent:
    """Persiste un événement ``account.*`` avec schéma fermé uniquement.

    Aucune valeur rejetée n'est recopiée ailleurs. ``request.META`` est refusé.
    """
    # Abandon silencieux de toute entrée META — sans journaliser les valeurs.
    safe_extra: dict[str, Any] = {}
    if isinstance(detail, dict):
        for key, value in detail.items():
            key_s = str(key)
            if key_s.startswith("HTTP_") or key_s == "REMOTE_ADDR":
                continue
            safe_extra[key_s] = value

    candidate = {
        **safe_extra,
        "action": action,
        "success": bool(success),
        "reason": reason,
        "target_user_id": getattr(target_user, "pk", None),
        "session_revoked": bool(
            safe_extra.get("session_revoked", action == "sessions_revoked")
        ),
    }
    # Second filtre : schéma fermé exclusif avant AuditEvent.objects.create.
    payload = build_closed_account_detail(candidate)
    return AuditEvent.objects.create(
        actor=actor,
        event_type=f"account.{action}" if _as_action(action) else "account.unknown",
        object_type="user",
        object_id=str(getattr(target_user, "pk", "") or "unknown"),
        detail=payload,
    )
