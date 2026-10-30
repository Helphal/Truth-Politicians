"""Anti-bruteforce connexion — sans identifiant réseau persisté (BUILD-003 R2).

Clé durable : pseudonyme normalisé / compte uniquement (HMAC), ou bucket global.
Politique temporelle bornée (compte + global) :
- les échecs sont comptés dans une fenêtre glissante ``window_started_at`` ;
- à l'expiration du verrou ou de la fenêtre, le compteur est réinitialisé ;
- un seul échec après expiration ne relance pas un verrou global immédiat.
État réseau éventuel : strictement éphémère en mémoire processus, jamais en base.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from atelier.models import LoginThrottle

# Scopes HMAC stables — jamais d'adresse, XFF, UA ou empreinte appareil.
_SCOPE_ACCOUNT = "dv.login.account.v2"
_SCOPE_GLOBAL = "dv.login.global.v2"

ACCOUNT_FAILURE_LIMIT = 5
ACCOUNT_LOCK_MINUTES = 15
ACCOUNT_WINDOW_MINUTES = 15

GLOBAL_FAILURE_LIMIT = 40
GLOBAL_LOCK_MINUTES = 5
GLOBAL_WINDOW_MINUTES = 5

# Mémoire processus uniquement — jamais écrite dans LoginThrottle / audit.
_EPHEMERAL_NET: dict[str, tuple[int, float]] = {}
_EPHEMERAL_NET_LIMIT = 30
_EPHEMERAL_NET_TTL_SEC = 120.0
_EPHEMERAL_NET_MAX_KEYS = 512


def normalize_login_identifier(username: str | None) -> str:
    return (username or "").strip().casefold()


def account_throttle_digest(username: str | None) -> str:
    """Condensat durable dérivé uniquement du pseudonyme normalisé."""
    material = f"{_SCOPE_ACCOUNT}|{normalize_login_identifier(username)}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), material, hashlib.sha256).hexdigest()


def global_throttle_digest() -> str:
    """Bucket global temporel — aucune donnée réseau."""
    material = _SCOPE_GLOBAL.encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _ephemeral_net_bucket(request) -> str | None:
    """Identifiant éphémère non persisté ; None si aucune donnée réseau."""
    remote = (request.META.get("REMOTE_ADDR") or "").strip()
    if not remote:
        return None
    # Condensat local uniquement pour la map mémoire ; jamais stocké.
    return hashlib.sha256(f"ephemeral-net|{remote}".encode("utf-8")).hexdigest()[:32]


def _ephemeral_locked(request) -> bool:
    bucket = _ephemeral_net_bucket(request)
    if not bucket:
        return False
    entry = _EPHEMERAL_NET.get(bucket)
    if not entry:
        return False
    failures, locked_until = entry
    now = time.monotonic()
    if locked_until > now:
        return True
    if locked_until and locked_until <= now:
        _EPHEMERAL_NET.pop(bucket, None)
    return False


def _ephemeral_record_failure(request) -> None:
    bucket = _ephemeral_net_bucket(request)
    if not bucket:
        return
    if len(_EPHEMERAL_NET) >= _EPHEMERAL_NET_MAX_KEYS:
        # Éviction simple des plus anciens déverrouillés.
        now = time.monotonic()
        stale = [k for k, (_, until) in _EPHEMERAL_NET.items() if until <= now]
        for key in stale[:64]:
            _EPHEMERAL_NET.pop(key, None)
        if len(_EPHEMERAL_NET) >= _EPHEMERAL_NET_MAX_KEYS:
            _EPHEMERAL_NET.clear()
    failures, locked_until = _EPHEMERAL_NET.get(bucket, (0, 0.0))
    now = time.monotonic()
    if locked_until and locked_until <= now:
        failures, locked_until = 0, 0.0
    failures += 1
    if failures >= _EPHEMERAL_NET_LIMIT:
        locked_until = now + _EPHEMERAL_NET_TTL_SEC
    _EPHEMERAL_NET[bucket] = (failures, locked_until)


def _ephemeral_clear(request) -> None:
    bucket = _ephemeral_net_bucket(request)
    if bucket:
        _EPHEMERAL_NET.pop(bucket, None)


def reset_ephemeral_login_state() -> None:
    """Tests uniquement : vide la map mémoire processus (non durable)."""
    _EPHEMERAL_NET.clear()


def _row_locked(digest: str) -> bool:
    attempt = LoginThrottle.objects.filter(key_digest=digest).first()
    return bool(attempt and attempt.locked_until and attempt.locked_until > timezone.now())


def is_login_throttled(request) -> bool:
    if _ephemeral_locked(request):
        return True
    username = request.POST.get("username", "")
    if _row_locked(account_throttle_digest(username)):
        return True
    if _row_locked(global_throttle_digest()):
        return True
    return False


def _reset_counter_if_needed(attempt: LoginThrottle, *, window_minutes: int, now) -> None:
    """Réinitialise le compteur si le verrou ou la fenêtre temporelle a expiré."""
    if attempt.locked_until and attempt.locked_until <= now:
        attempt.failures = 0
        attempt.locked_until = None
        attempt.window_started_at = now
        return
    if attempt.window_started_at is None:
        attempt.window_started_at = now
        return
    if now - attempt.window_started_at >= timedelta(minutes=window_minutes):
        # Fenêtre écoulée sans verrou actif : les échecs espacés ne cumulent pas à vie.
        attempt.failures = 0
        attempt.window_started_at = now


def _record_failure_on_row(
    *,
    key_digest: str,
    limit: int,
    lock_minutes: int,
    window_minutes: int,
) -> LoginThrottle:
    now = timezone.now()
    attempt, created = LoginThrottle.objects.get_or_create(
        key_digest=key_digest,
        defaults={"failures": 0, "window_started_at": now},
    )
    if created:
        attempt.failures = 1
        if attempt.failures >= limit:
            attempt.locked_until = now + timedelta(minutes=lock_minutes)
        attempt.save()
        return attempt

    _reset_counter_if_needed(attempt, window_minutes=window_minutes, now=now)
    if attempt.window_started_at is None:
        attempt.window_started_at = now
    attempt.failures += 1
    if attempt.failures >= limit:
        attempt.locked_until = now + timedelta(minutes=lock_minutes)
    attempt.save()
    return attempt


@transaction.atomic
def record_login_failure(request) -> None:
    _ephemeral_record_failure(request)
    username = request.POST.get("username", "")
    _record_failure_on_row(
        key_digest=account_throttle_digest(username),
        limit=ACCOUNT_FAILURE_LIMIT,
        lock_minutes=ACCOUNT_LOCK_MINUTES,
        window_minutes=ACCOUNT_WINDOW_MINUTES,
    )
    _record_failure_on_row(
        key_digest=global_throttle_digest(),
        limit=GLOBAL_FAILURE_LIMIT,
        lock_minutes=GLOBAL_LOCK_MINUTES,
        window_minutes=GLOBAL_WINDOW_MINUTES,
    )


@transaction.atomic
def clear_login_throttle(request) -> None:
    _ephemeral_clear(request)
    username = request.POST.get("username", "")
    LoginThrottle.objects.filter(key_digest=account_throttle_digest(username)).delete()


def persisted_throttle_digests() -> set[str]:
    return set(LoginThrottle.objects.values_list("key_digest", flat=True))


def assert_no_network_material_persisted(
    *,
    digests: set[str] | None = None,
    remotes: list[str] | None = None,
    user_agents: list[str] | None = None,
) -> None:
    """Recette : aucune valeur réseau ni dérivé trivial ne doit figurer en base."""
    digests = digests if digests is not None else persisted_throttle_digests()
    remotes = remotes or []
    user_agents = user_agents or []
    for digest in digests:
        assert len(digest) == 64
        for remote in remotes:
            assert remote not in digest
            # Le digest ne doit pas être un HMAC(secret, remote|…) de l'ancienne formule.
            legacy = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                f"{remote}|".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            assert digest != legacy
        for ua in user_agents:
            assert ua.casefold() not in digest.casefold()
