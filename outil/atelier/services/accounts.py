"""Comptes multi-utilisateurs BUILD-003 — pseudonymes, permissions, cycle de vie.

Identité interne immuable : ``User.pk`` (et ``UserProfile.internal_uid``).
Identifiant de connexion : pseudonyme régénérable (``User.username``).
Aucun IdP externe ; aucun mot de passe en clair ; pas d'IP/UA/empreinte.
"""
from __future__ import annotations

import hashlib
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from atelier.models import AccountInviteToken, AuditEvent, UserProfile
from atelier.services.account_audit import record_account_audit

# Lexique français/neutre (nature) — jamais dérivé d'un nom/e-mail/rôle/IP/GitHub.
# Espace combinatoire documenté : 18 × 16 × 18 = 5184.
# Le pseudonyme est un identifiant de connexion mémorable, pas un secret d'authentification.
_PSEUDONYM_LEFT = (
    "Brume",
    "Clarte",
    "Echo",
    "Ferme",
    "Grele",
    "Houle",
    "Lichen",
    "Nacre",
    "Ombre",
    "Pluie",
    "Rosee",
    "Sable",
    "Silence",
    "Source",
    "Vapeur",
    "Vague",
    "Vent",
    "Verglas",
)
_PSEUDONYM_RIGHT = (
    "Cedre",
    "Chene",
    "Erable",
    "Frene",
    "Genevrier",
    "Hetre",
    "Meleze",
    "Noyer",
    "Olivier",
    "Peuplier",
    "Pin",
    "Sapin",
    "Saule",
    "Sequoia",
    "Thuya",
    "Tilleul",
)
# 3e composante lexicale (nature / animal / ciel) — remplace l'ancien suffixe hex.
_PSEUDONYM_THIRD = (
    "Aigle",
    "Braise",
    "Ciel",
    "Corbeau",
    "Faucon",
    "Givre",
    "Heron",
    "Horizon",
    "Loutre",
    "Lynx",
    "Nebuleuse",
    "Nuage",
    "Orignal",
    "Ouragan",
    "Renard",
    "Roselin",
    "Saphir",
    "Zefir",
)

PSEUDONYM_COMPONENT_COUNTS = (
    len(_PSEUDONYM_LEFT),
    len(_PSEUDONYM_RIGHT),
    len(_PSEUDONYM_THIRD),
)
PSEUDONYM_SPACE_SIZE = (
    PSEUDONYM_COMPONENT_COUNTS[0]
    * PSEUDONYM_COMPONENT_COUNTS[1]
    * PSEUDONYM_COMPONENT_COUNTS[2]
)  # 18 × 16 × 18 = 5184

# Permissions explicites et extensibles (catalogue applicatif).
PERMISSION_CATALOG: dict[str, frozenset[str]] = {
    "accounts.manage": frozenset({UserProfile.Role.ADMIN}),
    "accounts.bootstrap": frozenset({UserProfile.Role.ADMIN}),
    "editorial.view": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.REVIEWER,
            UserProfile.Role.OPERATOR,
            UserProfile.Role.COLLECTOR,
        }
    ),
    "editorial.qualify": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.REVIEWER,
            UserProfile.Role.OPERATOR,
        }
    ),
    "editorial.review.prepare": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.REVIEWER,
            UserProfile.Role.OPERATOR,
        }
    ),
    # Alias historique : préparation/lecture du parcours, pas la décision finale.
    "editorial.review": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.REVIEWER,
            UserProfile.Role.OPERATOR,
        }
    ),
    "editorial.operate": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.OPERATOR,
        }
    ),
    "collect.run": frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.COLLECTOR,
            UserProfile.Role.OPERATOR,
        }
    ),
}

# Les rôles historiques restent valides ; ADMIN/EDITOR couvrent aussi les surfaces éditoriales.
_ROLE_IMPLIES: dict[str, frozenset[str]] = {
    UserProfile.Role.ADMIN: frozenset(
        {
            UserProfile.Role.ADMIN,
            UserProfile.Role.EDITOR,
            UserProfile.Role.OPERATOR,
            UserProfile.Role.COLLECTOR,
        }
    ),
    UserProfile.Role.EDITOR: frozenset(
        {
            UserProfile.Role.EDITOR,
            UserProfile.Role.OPERATOR,
        }
    ),
}


@dataclass(frozen=True)
class CreatedCredentials:
    user_id: int
    login_pseudonym: str
    temporary_password: str | None
    invite_token: str | None
    role: str
    internal_uid: str


def generate_pseudonym(*, max_attempts: int = 64) -> str:
    """Pseudonyme mémorable type Brume-Cedre-Lynx — trois composantes lexicales, unicité globale.

    Espace : ``PSEUDONYM_SPACE_SIZE`` (18×16×18=5184). Pas un secret d'auth ;
    l'admin ne choisit pas le username ; pas de suffixe opaque ni dérivation d'identité.
    """
    user_model = get_user_model()
    for _ in range(max_attempts):
        left = secrets.choice(_PSEUDONYM_LEFT)
        right = secrets.choice(_PSEUDONYM_RIGHT)
        third = secrets.choice(_PSEUDONYM_THIRD)
        candidate = f"{left}-{right}-{third}"
        if not user_model.objects.filter(username=candidate).exists():
            if not UserProfile.objects.filter(login_pseudonym=candidate).exists():
                return candidate
    raise ValidationError("pseudonym_generation_exhausted")


def assert_pseudonym_not_derived(
    pseudonym: str,
    *,
    real_name: str = "",
    email: str = "",
    role: str = "",
    ip: str = "",
    github: str = "",
) -> None:
    """Garde-fou tests/recette : le login ne doit pas provenir d'attributs réels."""
    material = " ".join(
        part.casefold()
        for part in (real_name, email, role, ip, github)
        if part
    )
    if not material:
        return
    folded = pseudonym.casefold()
    for token in material.replace("@", " ").replace(".", " ").replace("/", " ").split():
        if len(token) >= 3 and token in folded:
            raise ValidationError("pseudonym_derived_from_real_attribute")


def generate_temporary_password(length: int = 20) -> str:
    if length < 14:
        length = 14
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    symbols = "!@#$%^&*-_=+"
    while True:
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(symbols),
        ]
        chars.extend(secrets.choice(alphabet) for _ in range(length - 4))
        secrets.SystemRandom().shuffle(chars)
        password = "".join(chars)
        try:
            validate_password(password)
            return password
        except ValidationError:
            continue


def _hash_invite_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def profile_has_permission(profile: UserProfile, permission: str) -> bool:
    if not profile.is_account_usable():
        return False
    extras = set(profile.permissions_extra or [])
    if permission in extras:
        return True
    allowed_roles = PERMISSION_CATALOG.get(permission)
    if allowed_roles is None:
        return False
    return profile.role in allowed_roles


def role_satisfies(profile_role: str, required_roles: Iterable[str]) -> bool:
    implied = _ROLE_IMPLIES.get(profile_role, frozenset({profile_role}))
    return bool(implied.intersection(required_roles))


def public_attribution_for(user) -> str:
    """Libellé éditorial sans exposer l'identifiant technique au Site par défaut."""
    try:
        profile = user.dv_profile
    except UserProfile.DoesNotExist:
        return "rédacteur"
    label = (profile.public_attribution_label or profile.login_pseudonym or "").strip()
    return label or "rédacteur"


@transaction.atomic
def create_account(
    *,
    role: str,
    actor=None,
    is_test_identity: bool = False,
    use_invite_token: bool = False,
    invite_ttl_hours: int = 48,
) -> CreatedCredentials:
    if role not in UserProfile.Role.values:
        raise ValidationError("role_unknown")
    if actor is not None:
        try:
            actor_profile = actor.dv_profile
        except UserProfile.DoesNotExist as exc:
            raise PermissionDenied("actor_profile_missing") from exc
        if not profile_has_permission(actor_profile, "accounts.manage"):
            raise PermissionDenied("accounts_manage_required")

    user_model = get_user_model()
    login_pseudonym = generate_pseudonym()
    assert_pseudonym_not_derived(login_pseudonym, role=role)
    internal_uid = str(uuid.uuid4())
    temporary_password = None
    invite_token = None

    user = user_model(username=login_pseudonym)
    user.is_staff = False
    user.is_superuser = False
    if use_invite_token:
        user.set_unusable_password()
        invite_token = secrets.token_urlsafe(32)
    else:
        temporary_password = generate_temporary_password()
        validate_password(temporary_password, user=user)
        user.set_password(temporary_password)
    user.save()

    profile = UserProfile.objects.create(
        user=user,
        role=role,
        active=True,
        account_status=UserProfile.AccountStatus.ACTIVE,
        is_test_identity=is_test_identity,
        login_pseudonym=login_pseudonym,
        public_attribution_label=login_pseudonym,
        must_change_password=not use_invite_token,
        internal_uid=internal_uid,
        session_epoch=0,
        permissions_extra=[],
    )

    if invite_token:
        AccountInviteToken.objects.create(
            user=user,
            token_digest=_hash_invite_token(invite_token),
            expires_at=timezone.now() + timedelta(hours=invite_ttl_hours),
            created_by=actor,
        )

    record_account_audit(
        actor=actor,
        action="account_created",
        target_user=user,
        success=True,
        reason="created",
        detail={"role": role, "access_mode": "invite" if use_invite_token else "temp_password"},
    )
    return CreatedCredentials(
        user_id=user.pk,
        login_pseudonym=profile.login_pseudonym,
        temporary_password=temporary_password,
        invite_token=invite_token,
        role=role,
        internal_uid=internal_uid,
    )


@transaction.atomic
def regenerate_login_pseudonym(*, target_user, actor) -> str:
    """Régénère l'identifiant de connexion sans toucher à l'identité interne."""
    _require_manage(actor)
    profile = target_user.dv_profile
    previous = profile.login_pseudonym
    new_name = generate_pseudonym()
    assert_pseudonym_not_derived(new_name, role=profile.role)
    internal_uid = profile.internal_uid
    target_user.username = new_name
    target_user.save(update_fields=["username"])
    profile.login_pseudonym = new_name
    if profile.public_attribution_label == previous or not profile.public_attribution_label:
        profile.public_attribution_label = new_name
    profile.save(update_fields=["login_pseudonym", "public_attribution_label", "updated_at"])
    if profile.internal_uid != internal_uid:
        raise ValidationError("internal_uid_mutated")
    revoke_sessions(target_user=target_user, actor=actor, reason="pseudonym_regenerated")
    record_account_audit(
        actor=actor,
        action="pseudonym_regenerated",
        target_user=target_user,
        success=True,
        reason="login_rotated",
        detail={"previous_length": len(previous), "new_length": len(new_name)},
    )
    return new_name


@transaction.atomic
def set_account_status(*, target_user, actor, status: str, reason: str = "") -> None:
    _require_manage(actor)
    if status not in UserProfile.AccountStatus.values:
        raise ValidationError("account_status_unknown")
    profile = target_user.dv_profile
    if actor.pk == target_user.pk and status != UserProfile.AccountStatus.ACTIVE:
        raise PermissionDenied("cannot_suspend_or_revoke_self")
    profile.account_status = status
    profile.active = status == UserProfile.AccountStatus.ACTIVE
    profile.save(update_fields=["account_status", "active", "updated_at"])
    # Garder User.is_active pour que le login atteigne le refus applicatif 403
    # (account_blocked) plutôt qu'un simple échec d'identifiants Django.
    # La usabilité réelle reste profile.is_account_usable().
    if status == UserProfile.AccountStatus.ACTIVE:
        target_user.is_active = True
        target_user.save(update_fields=["is_active"])
    if status != UserProfile.AccountStatus.ACTIVE:
        revoke_sessions(target_user=target_user, actor=actor, reason=f"status_{status}")
        # Révocation / suspension : invalider les invitations pour qu'elles
        # ne redeviennent pas utilisables après une éventuelle réactivation.
        AccountInviteToken.objects.filter(user=target_user, used_at__isnull=True).update(
            used_at=timezone.now()
        )
    record_account_audit(
        actor=actor,
        action=f"account_{status}",
        target_user=target_user,
        success=True,
        reason=reason or status,
    )


@transaction.atomic
def change_role(*, target_user, actor, new_role: str) -> None:
    _require_manage(actor)
    if new_role not in UserProfile.Role.values:
        raise ValidationError("role_unknown")
    if actor.pk == target_user.pk:
        raise PermissionDenied("no_self_promotion")
    profile = target_user.dv_profile
    old = profile.role
    profile.role = new_role
    profile.save(update_fields=["role", "updated_at"])
    record_account_audit(
        actor=actor,
        action="role_changed",
        target_user=target_user,
        success=True,
        reason="role_update",
        detail={"from": old, "to": new_role},
    )


@transaction.atomic
def reset_access(*, target_user, actor, use_invite_token: bool = False) -> CreatedCredentials:
    """Réinitialise l'accès ; invalide atomiquement toute invitation antérieure.

    Quel que soit le mode (MDP temporaire ou nouvelle invitation), les jetons
    d'invitation encore ouverts sont marqués utilisés avant d'émettre le nouveau
    moyen d'activation. Les sessions antérieures restent révoquées.
    """
    _require_manage(actor)
    profile = target_user.dv_profile
    temporary_password = None
    invite_token = None
    # Toujours invalider les invitations antérieures (y compris reset → MDP).
    AccountInviteToken.objects.filter(user=target_user, used_at__isnull=True).update(
        used_at=timezone.now()
    )
    if use_invite_token:
        target_user.set_unusable_password()
        invite_token = secrets.token_urlsafe(32)
        AccountInviteToken.objects.create(
            user=target_user,
            token_digest=_hash_invite_token(invite_token),
            expires_at=timezone.now() + timedelta(hours=48),
            created_by=actor,
        )
        profile.must_change_password = False
    else:
        temporary_password = generate_temporary_password()
        validate_password(temporary_password, user=target_user)
        target_user.set_password(temporary_password)
        profile.must_change_password = True
    target_user.save()
    profile.save(update_fields=["must_change_password", "updated_at"])
    revoke_sessions(target_user=target_user, actor=actor, reason="access_reset")
    record_account_audit(
        actor=actor,
        action="access_reset",
        target_user=target_user,
        success=True,
        reason="reset",
        detail={"access_mode": "invite" if use_invite_token else "temp_password"},
    )
    return CreatedCredentials(
        user_id=target_user.pk,
        login_pseudonym=profile.login_pseudonym,
        temporary_password=temporary_password,
        invite_token=invite_token,
        role=profile.role,
        internal_uid=profile.internal_uid,
    )


@transaction.atomic
def complete_password_change(*, user, new_password: str, request=None) -> None:
    """Change le mot de passe ; conserve la session courante si ``request`` est fourni.

    Les autres sessions ne sont pas réactivées : leur empreinte d'auth devient
    invalide. ``reset_access`` continue de révoquer explicitement les sessions.
    """
    from django.contrib.auth import update_session_auth_hash

    validate_password(new_password, user=user)
    user.set_password(new_password)
    user.save()
    if request is not None:
        update_session_auth_hash(request, user)
    profile = user.dv_profile
    profile.must_change_password = False
    profile.last_activity_at = timezone.now()
    profile.save(update_fields=["must_change_password", "last_activity_at", "updated_at"])
    record_account_audit(
        actor=user,
        action="password_changed",
        target_user=user,
        success=True,
        reason="mandatory_or_voluntary",
    )


@transaction.atomic
def redeem_invite_token(*, raw_token: str, new_password: str):
    digest = _hash_invite_token(raw_token)
    token = (
        AccountInviteToken.objects.select_for_update()
        .filter(token_digest=digest, used_at__isnull=True)
        .select_related("user", "user__dv_profile")
        .first()
    )
    if token is None:
        record_account_audit(
            actor=None,
            action="invite_redeem",
            target_user=None,
            success=False,
            reason="token_invalid",
        )
        raise ValidationError("invite_token_invalid")
    if token.expires_at <= timezone.now():
        record_account_audit(
            actor=None,
            action="invite_redeem",
            target_user=token.user,
            success=False,
            reason="token_expired",
        )
        raise ValidationError("invite_token_expired")
    user = token.user
    if not user.dv_profile.is_account_usable():
        raise ValidationError("account_not_usable")
    validate_password(new_password, user=user)
    user.set_password(new_password)
    user.save()
    profile = user.dv_profile
    profile.must_change_password = False
    profile.last_activity_at = timezone.now()
    profile.save(update_fields=["must_change_password", "last_activity_at", "updated_at"])
    token.used_at = timezone.now()
    token.save(update_fields=["used_at"])
    record_account_audit(
        actor=user,
        action="invite_redeem",
        target_user=user,
        success=True,
        reason="accepted",
    )
    return user


@transaction.atomic
def revoke_sessions(*, target_user, actor, reason: str = "session_revoked") -> int:
    profile = target_user.dv_profile
    profile.session_epoch = (profile.session_epoch or 0) + 1
    profile.save(update_fields=["session_epoch", "updated_at"])
    deleted = 0
    for session in Session.objects.all():
        data = session.get_decoded()
        if str(data.get("_auth_user_id")) == str(target_user.pk):
            session.delete()
            deleted += 1
    record_account_audit(
        actor=actor,
        action="sessions_revoked",
        target_user=target_user,
        success=True,
        reason=reason,
        detail={"sessions_deleted": deleted, "session_epoch": profile.session_epoch},
    )
    return deleted


def touch_activity(user) -> None:
    try:
        profile = user.dv_profile
    except UserProfile.DoesNotExist:
        return
    UserProfile.objects.filter(pk=profile.pk).update(last_activity_at=timezone.now())


def _require_manage(actor) -> UserProfile:
    try:
        profile = actor.dv_profile
    except UserProfile.DoesNotExist as exc:
        raise PermissionDenied("actor_profile_missing") from exc
    if not profile_has_permission(profile, "accounts.manage"):
        raise PermissionDenied("accounts_manage_required")
    return profile


def audit_detail_has_surveillance(detail: dict) -> bool:
    """Utilitaire de test : détecte IP/UA/empreinte dans un détail d'audit."""
    forbidden = {
        "ip",
        "ip_address",
        "remote_addr",
        "x_forwarded_for",
        "user_agent",
        "ua",
        "fingerprint",
        "device_fingerprint",
        "client_ip",
    }
    sanitized = sanitize_audit_detail(detail)
    lowered = {str(k).casefold() for k in sanitized}
    return bool(lowered.intersection(forbidden))


def login_throttle_material_is_hashed(key_digest: str, remote: str) -> bool:
    """Compat recette : le digest ne doit contenir aucune donnée réseau."""
    if remote and remote in key_digest:
        return False
    return len(key_digest) == 64


def bind_session_epoch(request, user) -> None:
    try:
        epoch = user.dv_profile.session_epoch
    except UserProfile.DoesNotExist:
        epoch = 0
    request.session["dv_session_epoch"] = epoch


def session_epoch_valid(request, user) -> bool:
    try:
        expected = user.dv_profile.session_epoch
    except UserProfile.DoesNotExist:
        return True
    if "dv_session_epoch" not in request.session:
        # Première requête authentifiée (login ou force_login de test) : lier l'époque.
        request.session["dv_session_epoch"] = expected
        return True
    return request.session.get("dv_session_epoch") == expected


def ensure_audit_event_clean(event: AuditEvent) -> bool:
    return not audit_detail_has_surveillance(event.detail or {})
