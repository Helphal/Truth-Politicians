"""Même invariant aux frontières normalisées et contractuelles. Inconnu ≠ faux."""
from datetime import date
from django.core.exceptions import ValidationError


def validate_mandate_link(*, person_id, mandate_person_id, institution,
                          mandate_institution, occurred_on, starts_on, ends_on=None,
                          date_precision="day"):
    if person_id != mandate_person_id:
        raise ValidationError("vote_mandate_person_mismatch")
    if institution != mandate_institution:
        raise ValidationError("vote_mandate_institution_mismatch")
    # Une date approximative ne justifie pas une appartenance à un jour précis.
    if date_precision != "day":
        raise ValidationError("vote_mandate_date_not_precise")
    try:
        day = date.fromisoformat(str(occurred_on))
        start = date.fromisoformat(str(starts_on))
        end = date.fromisoformat(str(ends_on)) if ends_on else None
    except (ValueError, TypeError) as exc:
        raise ValidationError("vote_mandate_date_invalid") from exc
    if day < start or (end and (end < start or day > end)):
        raise ValidationError("vote_mandate_outside_period")
