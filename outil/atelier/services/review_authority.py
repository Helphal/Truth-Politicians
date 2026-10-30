"""Autorité de revue finale discours–votes (identité interne immuable, refus par défaut)."""
from __future__ import annotations

from django.conf import settings

from atelier.models import Environment, UserProfile
from atelier.services.accounts import profile_has_permission


def configured_final_review_authority_uid() -> str:
    return (getattr(settings, "DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID", "") or "").strip()


def has_final_discourse_review_authority(profile: UserProfile, environment: str) -> bool:
    if not profile.is_account_usable():
        return False
    if environment == Environment.PRODUCTION and profile.is_test_identity:
        return False
    authority_uid = configured_final_review_authority_uid()
    if not authority_uid:
        return False
    return str(profile.internal_uid) == authority_uid


def may_access_detached_review_surface(profile: UserProfile, environment: str) -> bool:
    """Signature fictive / revue détachée : titulaire final uniquement, pas le parcours préparateur."""
    if not profile.is_account_usable():
        return False
    authority_uid = configured_final_review_authority_uid()
    if not authority_uid:
        return False
    return str(profile.internal_uid) == authority_uid


def may_record_human_review_decision(profile: UserProfile, decision: str, environment: str) -> bool:
    from atelier.models import HumanReview

    if decision in {HumanReview.Decision.APPROVE, HumanReview.Decision.REJECT}:
        return has_final_discourse_review_authority(profile, environment)
    return profile_has_permission(profile, "editorial.qualify")
