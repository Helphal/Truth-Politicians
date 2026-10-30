"""Écritures auditées et immuables des relations du comparateur thématique."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from atelier.models import DeclarationMeasureLink, MeasureScrutinyLink


@transaction.atomic
def record_declaration_measure_link(*, user, declaration, measure, stance, qualification_status, evidence, rationale):
    current = (
        DeclarationMeasureLink.objects.select_for_update()
        .filter(declaration=declaration, measure=measure)
        .aggregate(value=Max("revision"))["value"]
        or 0
    )
    item = DeclarationMeasureLink(
        declaration=declaration,
        measure=measure,
        revision=current + 1,
        stance=stance,
        qualification_status=qualification_status,
        evidence=evidence,
        rationale=rationale,
        recorded_by=user,
    )
    item.full_clean()
    item.save()
    return item


@transaction.atomic
def record_measure_scrutiny_link(*, user, measure, scrutiny, vote_effect, qualification_status, evidence, rationale):
    current = (
        MeasureScrutinyLink.objects.select_for_update()
        .filter(measure=measure, scrutiny=scrutiny)
        .aggregate(value=Max("revision"))["value"]
        or 0
    )
    item = MeasureScrutinyLink(
        measure=measure,
        scrutiny=scrutiny,
        revision=current + 1,
        vote_effect=vote_effect,
        qualification_status=qualification_status,
        evidence=evidence,
        rationale=rationale,
        recorded_by=user,
    )
    item.full_clean()
    item.save()
    return item
