"""Frontière d'entrée privée pour capacités admin (PRIVATE-ADMIN-1).

Autorisation fondée UNIQUEMENT sur un signal injecté/écrasé par un proxy de
confiance (``DV_TRUST_PRIVATE_ENTRY_PROXY=1``), jamais sur ``Host`` ni
``X-Forwarded-For`` fournis par un client public.

Ce lot cloud prépare code + tests + contrat. L'admin « local/Tailscale seulement »
n'est EFFECTIF qu'après contrôle NAS réel (proxy/ACL) — hors périmètre ici.
"""
from __future__ import annotations

import hmac

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import render

from atelier.models import UserProfile
from atelier.services.account_audit import record_account_audit

# Routes / préfixes réservés à l'entrée privée (admin et gestion de comptes).
PRIVATE_ADMIN_URL_NAMES = frozenset(
    {
        "accounts_list",
        "accounts_create",
        "accounts_action",
    }
)
PRIVATE_ADMIN_PATH_PREFIXES = (
    "/comptes/",
    "/administration-technique/",
)


def is_admin_account(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    try:
        return user.dv_profile.role == UserProfile.Role.ADMIN
    except UserProfile.DoesNotExist:
        return False


def private_entry_configured() -> bool:
    mode = getattr(settings, "DV_PRIVATE_ENTRY_MODE", "closed")
    if mode == "open":
        return settings.DV_ENV in {"local", "test"}
    if mode == "header":
        return bool(getattr(settings, "DV_TRUST_PRIVATE_ENTRY_PROXY", False))
    return False


def is_private_admin_entry(request) -> bool:
    """True si la requête arrive via l'entrée privée authentifiée par le proxy."""
    mode = getattr(settings, "DV_PRIVATE_ENTRY_MODE", "closed")
    if mode == "open":
        # Uniquement local/test : jamais en private/production.
        return settings.DV_ENV in {"local", "test"}
    if mode != "header":
        return False
    if not getattr(settings, "DV_TRUST_PRIVATE_ENTRY_PROXY", False):
        return False
    meta_key = getattr(settings, "DV_PRIVATE_ENTRY_HEADER_META", "HTTP_X_DV_PRIVATE_ENTRY")
    expected = getattr(settings, "DV_PRIVATE_ENTRY_EXPECTED_VALUE", "1")
    provided = request.META.get(meta_key, "")
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(provided, expected)


def privileged_path(request) -> bool:
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None) if match else None
    if url_name is None:
        try:
            from django.urls import resolve

            url_name = resolve(request.path_info).url_name
        except Exception:
            url_name = None
    if url_name in PRIVATE_ADMIN_URL_NAMES:
        return True
    path = request.path or ""
    return any(path.startswith(prefix) for prefix in PRIVATE_ADMIN_PATH_PREFIXES)


def refuse_admin_on_public(request, *, reason: str = "admin_public_entry_refused", target_user=None):
    user = target_user
    if user is None:
        candidate = getattr(request, "user", None)
        if candidate is not None and getattr(candidate, "is_authenticated", False):
            user = candidate
    record_account_audit(
        actor=None,
        action="admin_entry_guard",
        target_user=user,
        success=False,
        reason=reason,
        detail={"private_entry": False},
    )
    if getattr(request, "user", None) is not None and getattr(request.user, "is_authenticated", False):
        logout(request)
    return render(
        request,
        "atelier/error.html",
        {
            "message": (
                "Accès administrateur refusé hors entrée privée "
                "(LAN / Tailscale via proxy de confiance)."
            )
        },
        status=403,
    )


def bind_admin_private_session(request) -> None:
    request.session["dv_admin_bound_to_private"] = True
    request.session["dv_admin_session_marker"] = "private"


def admin_session_allowed_here(request) -> bool:
    """Session admin uniquement valable sur entrée privée ; inutilisable en public."""
    if not is_admin_account(request.user):
        return True
    if not is_private_admin_entry(request):
        return False
    # Lier la session à l'entrée privée (anti-réutilisation sur origine publique).
    if request.session.get("dv_admin_bound_to_private") is not True:
        bind_admin_private_session(request)
    return True


class PrivateAdminEntryMiddleware:
    """Garde-fou applicatif : capacités admin uniquement sur entrée privée prouvée."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.dv_private_entry = is_private_admin_entry(request)
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.dv_private_entry = is_private_admin_entry(request)
        user = getattr(request, "user", None)

        if user is not None and user.is_authenticated and is_admin_account(user):
            if not admin_session_allowed_here(request):
                return refuse_admin_on_public(request, reason="admin_session_on_public")

        if privileged_path(request):
            if not request.dv_private_entry:
                return refuse_admin_on_public(request, reason="privileged_path_public")

        return None

