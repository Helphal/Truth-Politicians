"""Autorité réseau commune, vérifiée avant DNS, sans repli direct implicite."""
from django.conf import settings


class NetworkBlocked(RuntimeError):
    pass


def require_direct_transport():
    mode = settings.DV_NETWORK_MODE
    if mode == "disabled":
        raise NetworkBlocked("network_disabled")
    if mode == "protected":
        # Les appelants qui savent seulement ouvrir une socket directe (par
        # exemple le pont WordPress) ne doivent jamais profiter implicitement
        # du transport de collecte protégé.
        raise NetworkBlocked("protected_transport_required_for_callsite")
    if mode != "direct":
        raise NetworkBlocked("network_mode_unknown")
    return "direct"


def require_collection_transport():
    """Autorise le direct explicite ou le relais OSINT protégé configuré."""
    mode = settings.DV_NETWORK_MODE
    if mode != "protected":
        return require_direct_transport()
    if not settings.DV_PROTECTED_EGRESS_CONFIRMED:
        raise NetworkBlocked("protected_egress_not_confirmed")
    if not settings.DV_PROTECTED_TRANSPORT_ENDPOINT:
        raise NetworkBlocked("protected_transport_endpoint_missing_no_direct_fallback")
    if settings.DV_PROTECTED_EGRESS_PROFILE not in {"TOR", "VPN_EXISTING"}:
        raise NetworkBlocked("protected_transport_profile_not_protected")
    return "protected"
