"""Contre-revue R3 SUITE-005 : empreinte FP-3 (dates) et consolidation collective.

Ne remplace pas et n'affaiblit pas pilotage/contre_revues/test_reception_r28.py.
Marqueur : DV-OUTIL-NEXT-2-2871225-R3.
"""
from datetime import date, timedelta

from django.test import override_settings

from atelier.models import (
    Affiliation,
    DataState,
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    Organization,
    Person,
    VoteObservation,
)
from atelier.services.thematic_comparisons import compare_declaration
from atelier.services.thematic_integrity import (
    FINGERPRINT_VERSION,
    collective_vote_distribution,
)
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_thematic_comparisons import ThematicComparisonTests


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Suite005R3DateFingerprintTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def rows(self, declaration):
        return compare_declaration(declaration_id=declaration.pk)["rows"]

    def assert_not_directional(self, declaration):
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )

    def requalify(self, declaration, measure, passage, *, revision, stance=DeclarationMeasureLink.Stance.SUPPORT):
        link = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=revision,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=stance,
            evidence=passage,
            rationale=f"Nouvelle qualification explicite révision {revision} après correction documentée.",
        )
        link.full_clean()
        link.save()
        return link

    def test_fingerprint_version_is_fp3(self):
        self.assertEqual(FINGERPRINT_VERSION, "DV-THEMATIC-FP-4")

    def test_changed_occurred_on_invalidates_qualification(self):
        declaration, _, _, _ = self.make_chain()
        declaration.occurred_on = declaration.occurred_on - timedelta(days=5)
        declaration.full_clean()
        declaration.save()
        self.assert_not_directional(declaration)

    def test_known_to_unknown_date_state_invalidates_qualification(self):
        declaration, _, _, _ = self.make_chain()
        declaration.occurred_on_state = DataState.UNKNOWN
        declaration.full_clean()
        declaration.save()
        self.assert_not_directional(declaration)

    def test_day_to_coarser_precision_invalidates_qualification(self):
        for precision in (
            Declaration.DatePrecision.YEAR,
            Declaration.DatePrecision.MONTH,
            Declaration.DatePrecision.ESTIMATED,
        ):
            with self.subTest(precision=precision):
                declaration, _, _, _ = self.make_chain()
                declaration.date_precision = precision
                declaration.full_clean()
                declaration.save()
                self.assert_not_directional(declaration)

    def test_explicit_requalification_after_date_correction_restores_comparison(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.occurred_on = declaration.occurred_on - timedelta(days=2)
        declaration.full_clean()
        declaration.save()
        self.assert_not_directional(declaration)
        self.requalify(declaration, measure, passage, revision=2)
        row = self.rows(declaration)[0]
        self.assertEqual(row["conclusion"]["code"], "convergence")
        self.assertEqual(row["declaration"]["revision"], 2)


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Suite005R3CollectiveAggregationTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def _group_setup(self):
        declaration, measure, scrutiny, passage = self.make_chain()
        org = Organization.objects.create(
            stable_id=self.unique("GROUP-R3"),
            name="Groupe R3",
            kind=Organization.Kind.PARLIAMENTARY_GROUP,
        )
        Affiliation.objects.create(
            person=declaration.speaker_person,
            organization=org,
            status="official_group_mandate",
            starts_on=date(2023, 1, 1),
            ends_on=date(2024, 12, 31),
            observed_on=date(2023, 6, 1),
            source_document=passage.document,
        )
        return declaration, measure, scrutiny, passage, org

    def test_two_concordant_observations_count_as_one_voice(self):
        declaration, _, scrutiny, passage, org = self._group_setup()
        second_document = self.make_document(source_id=self.unique("VOTE-ALT"))
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=declaration.speaker_person,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=second_document,
        )
        result = collective_vote_distribution(org, scrutiny)
        self.assertEqual(result["distribution"].get(VoteObservation.Category.FOR), 1)
        self.assertEqual(result["usable_voices"], 1)
        self.assertEqual(result["usable_observations"], 2)
        self.assertEqual(len(result["voices"][0]["sources"]), 2)

    def test_conflicting_categories_for_same_person_yield_no_directional_voice(self):
        declaration, _, scrutiny, passage, org = self._group_setup()
        second_document = self.make_document(source_id=self.unique("VOTE-CONFLICT"))
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=declaration.speaker_person,
            source_category="Contre",
            normalized_category=VoteObservation.Category.AGAINST,
            mapping_rule="TEST-EXACT",
            source_document=second_document,
        )
        result = collective_vote_distribution(org, scrutiny)
        self.assertEqual(result["distribution"], {})
        self.assertEqual(result["usable_voices"], 0)
        self.assertTrue(any(item["reason"] == "person_vote_conflict" for item in result["exclusions"]))

    def test_quarantined_source_is_excluded_while_usable_source_counts_once(self):
        declaration, _, scrutiny, passage, org = self._group_setup()
        quarantined = self.make_document(source_id=self.unique("VOTE-Q"))
        quarantined.quarantined = True
        quarantined.save()
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=declaration.speaker_person,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=quarantined,
        )
        result = collective_vote_distribution(org, scrutiny)
        self.assertEqual(result["distribution"].get(VoteObservation.Category.FOR), 1)
        self.assertEqual(result["usable_voices"], 1)
        self.assertTrue(any(item["reason"] == "source_unusable" for item in result["exclusions"]))

    def test_mixed_affiliates_concordant_conflict_and_missing(self):
        declaration, _, scrutiny, passage, org = self._group_setup()
        ally = Person.objects.create(
            stable_id=self.unique("PERSON-ALLY"),
            display_name="Alliée fictive",
            institutional_ids={"test": self.unique("ALLY")},
        )
        conflicted = Person.objects.create(
            stable_id=self.unique("PERSON-CF"),
            display_name="Élue en conflit",
            institutional_ids={"test": self.unique("CF")},
        )
        missing = Person.objects.create(
            stable_id=self.unique("PERSON-MISS"),
            display_name="Sans observation",
            institutional_ids={"test": self.unique("MISS")},
        )
        for person in (ally, conflicted, missing):
            Affiliation.objects.create(
                person=person,
                organization=org,
                status="official_group_mandate",
                starts_on=date(2023, 1, 1),
                ends_on=date(2024, 12, 31),
                observed_on=date(2023, 6, 1),
                source_document=passage.document,
            )
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=ally,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=passage.document,
        )
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=conflicted,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=passage.document,
        )
        alt = self.make_document(source_id=self.unique("VOTE-CF-ALT"))
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=conflicted,
            source_category="Contre",
            normalized_category=VoteObservation.Category.AGAINST,
            mapping_rule="TEST-EXACT",
            source_document=alt,
        )
        result = collective_vote_distribution(org, scrutiny)
        self.assertEqual(result["affiliations"], 4)
        self.assertEqual(result["usable_voices"], 2)
        self.assertEqual(result["distribution"].get(VoteObservation.Category.FOR), 2)
        self.assertTrue(any(item["reason"] == "person_vote_conflict" for item in result["exclusions"]))
        self.assertTrue(
            any(
                item["reason"] == "affiliation_without_usable_vote" and item.get("person_id") == missing.pk
                for item in result["exclusions"]
            )
        )

    def test_collective_spokesperson_separation_is_preserved(self):
        declaration, measure, scrutiny, passage, org = self._group_setup()
        declaration.speaker_organization = org
        declaration.capacity = "porte-parole du groupe"
        declaration.position_scope = Declaration.PositionScope.COLLECTIVE
        declaration.full_clean()
        declaration.save()
        link = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            rationale="Parole collective requalifiée après changement de portée.",
            evidence=passage,
        )
        link.full_clean()
        link.save()
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["vote"]["state"], "not_personal")
        self.assertEqual(row["conclusion"]["code"], "not_comparable")
        self.assertEqual(row["actors"]["compared_actor"]["kind"], "organization")
        self.assertEqual(row["collective"]["state"], "group_distribution")
        self.assertIn("pas la position personnelle", row["collective"]["reserve"])
