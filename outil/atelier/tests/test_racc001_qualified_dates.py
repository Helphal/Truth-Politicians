"""Dates qualifiées C01 — intervalle distinct de published_on."""
from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError

from atelier.models import DataState, Declaration
from atelier.services.thematic_integrity import (
    declaration_measure_material,
    fingerprint_declaration_measure_link,
    qualified_declaration_payload,
)
from atelier.models import DeclarationMeasureLink
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_thematic_comparisons import ThematicComparisonTests


class Racc001QualifiedIntervalTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self._thematic = ThematicComparisonTests()
        self._thematic.setUp()

    def make_chain(self, **kwargs):
        return self._thematic.make_chain(**kwargs)

    def _declaration(self, **overrides) -> Declaration:
        declaration, _, _, _ = self.make_chain(person=True, declaration_date=date(2026, 6, 1))
        for key, value in overrides.items():
            setattr(declaration, key, value)
        declaration.full_clean()
        declaration.save()
        return declaration

    def test_interval_end_uses_occurred_interval_end_not_publication(self):
        declaration = self._declaration(
            date_precision=Declaration.DatePrecision.INTERVAL,
            occurred_on=date(2026, 6, 1),
            occurred_interval_end=date(2026, 9, 30),
            published_on=date(2026, 12, 15),
        )
        qualified = qualified_declaration_payload(declaration)
        self.assertEqual(qualified["interval_start"], "2026-06-01")
        self.assertEqual(qualified["interval_end"], "2026-09-30")

    def test_interval_incomplete_without_qualified_end(self):
        declaration = self._declaration(
            date_precision=Declaration.DatePrecision.INTERVAL,
            occurred_on=date(2026, 6, 1),
            occurred_interval_end=None,
            published_on=date(2026, 9, 30),
        )
        qualified = qualified_declaration_payload(declaration)
        self.assertEqual(qualified["interval_start"], "2026-06-01")
        self.assertIsNone(qualified["interval_end"])

    def test_unknown_state_does_not_export_interval_bounds(self):
        declaration, _, _, _ = self.make_chain(person=True, declaration_date=date(2026, 6, 1))
        declaration.date_precision = Declaration.DatePrecision.INTERVAL
        declaration.occurred_on_state = DataState.UNKNOWN
        declaration.occurred_on = None
        declaration.occurred_interval_end = date(2026, 9, 30)
        declaration.save(
            update_fields=[
                "date_precision",
                "occurred_on_state",
                "occurred_on",
                "occurred_interval_end",
                "updated_at",
            ]
        )
        qualified = qualified_declaration_payload(declaration)
        self.assertIsNone(qualified["value"])
        self.assertIsNone(qualified["interval_start"])
        self.assertIsNone(qualified["interval_end"])

    def test_inverted_interval_rejected_on_clean(self):
        declaration, _, _, _ = self.make_chain(person=True, declaration_date=date(2026, 9, 30))
        declaration.date_precision = Declaration.DatePrecision.INTERVAL
        declaration.occurred_on = date(2026, 9, 30)
        declaration.occurred_interval_end = date(2026, 6, 1)
        with self.assertRaises(ValidationError):
            declaration.full_clean()

    def test_inverted_interval_rejected_at_qualified_projection(self):
        declaration, _, _, _ = self.make_chain(person=True, declaration_date=date(2026, 9, 30))
        declaration.date_precision = Declaration.DatePrecision.INTERVAL
        declaration.occurred_on_state = DataState.KNOWN
        declaration.occurred_on = date(2026, 9, 30)
        declaration.occurred_interval_end = date(2026, 6, 1)
        with self.assertRaises(ValidationError):
            qualified_declaration_payload(declaration)

    def test_unknown_state_keeps_stored_date_but_exports_no_bounds(self):
        declaration, _, _, _ = self.make_chain(person=True, declaration_date=date(2026, 3, 15))
        declaration.date_precision = Declaration.DatePrecision.INTERVAL
        declaration.occurred_on_state = DataState.UNKNOWN
        declaration.occurred_on = date(2026, 3, 15)
        declaration.occurred_interval_end = date(2026, 6, 1)
        declaration.save(
            update_fields=[
                "date_precision",
                "occurred_on_state",
                "occurred_on",
                "occurred_interval_end",
                "updated_at",
            ]
        )
        qualified = qualified_declaration_payload(declaration)
        self.assertIsNone(qualified["value"])
        self.assertIsNone(qualified["interval_start"])
        self.assertIsNone(qualified["interval_end"])

    def test_interval_end_change_invalidates_declaration_measure_fingerprint(self):
        declaration, measure, _, passage = self.make_chain(person=True, declaration_date=date(2026, 6, 1))
        declaration.date_precision = Declaration.DatePrecision.INTERVAL
        declaration.occurred_interval_end = date(2026, 9, 30)
        declaration.save(update_fields=["date_precision", "occurred_interval_end", "updated_at"])
        link = DeclarationMeasureLink.objects.filter(declaration=declaration, measure=measure).first()
        self.assertIsNotNone(link)
        fp_before = fingerprint_declaration_measure_link(link)
        declaration.occurred_interval_end = date(2026, 10, 31)
        material = declaration_measure_material(
            declaration=declaration,
            measure=measure,
            evidence=passage,
            stance=link.stance,
        )
        self.assertEqual(material["occurred_interval_end"], "2026-10-31")
        link.declaration = declaration
        fp_after = fingerprint_declaration_measure_link(link)
        self.assertNotEqual(fp_before, fp_after)
        self.assertFalse(link.content_fingerprint == fp_after)
