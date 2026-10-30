"""Validateurs de mots de passe renforcés — BUILD-003 / PRIVATE-ADMIN-1."""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class StrongPasswordComplexityValidator:
    """Exige majuscule, minuscule, chiffre et symbole — applicable partout."""

    def validate(self, password, user=None):
        missing = []
        if not re.search(r"[A-Z]", password or ""):
            missing.append("une majuscule")
        if not re.search(r"[a-z]", password or ""):
            missing.append("une minuscule")
        if not re.search(r"[0-9]", password or ""):
            missing.append("un chiffre")
        if not re.search(r"[^A-Za-z0-9]", password or ""):
            missing.append("un symbole")
        if missing:
            raise ValidationError(
                _("Mot de passe trop faible : ajoutez %(parts)s."),
                code="password_complexity",
                params={"parts": ", ".join(missing)},
            )

    def get_help_text(self):
        return _(
            "Votre mot de passe doit contenir au moins une majuscule, "
            "une minuscule, un chiffre et un symbole."
        )
