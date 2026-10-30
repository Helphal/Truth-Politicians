"""Cas voisins SUITE-005 : reprise, dates, acteurs, interface.

Ne remplace pas et n'affaiblit pas pilotage/contre_revues/test_reception_r28.py.
Aucun réseau, aucune revue réelle, aucune publication.
"""
from datetime import date

from django.test import override_settings
from django.urls import reverse

from atelier.models import (
    Affiliation,
    DataState,
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    EvidencePassage,
    Measure,
    MeasureScrutinyLink,
    Organization,
    Person,
    Source,
    VoteObservation,
)
from atelier.services.documentary_preparation import prepare_documentary_review
from atelier.services.research_cases import prepare_research_case
from atelier.services.thematic_comparisons import compare_declaration, compare_measure
from atelier.services.thematic_integrity import thematic_preparation_snapshot
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_thematic_comparisons import ThematicComparisonTests


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Suite005NeighboringTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def rows(self, declaration, **kwargs):
        return compare_declaration(declaration_id=declaration.pk, **kwargs)["rows"]

    def test_new_qualification_after_correction_restores_conclusion(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.exact_quote = "Je refuse désormais toute hausse du salaire minimum."
        declaration.paraphrase = "Opposition explicite après correction."
        declaration.full_clean()
        declaration.save()
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )
        restored = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.OPPOSE,
            evidence=passage,
            rationale="Nouvelle qualification explicite après correction documentée de la citation.",
        )
        restored.full_clean()
        restored.save()
        row = self.rows(declaration)[0]
        self.assertEqual(row["conclusion"]["code"], "tension")
        self.assertEqual(row["declaration"]["revision"], 2)

    def test_speaker_label_technical_change_does_not_invalidate(self):
        declaration, _, _, _ = self.make_chain()
        declaration.speaker_label = "Élue fictive (libellé d'affichage)"
        declaration.full_clean()
        declaration.save()
        self.assertEqual(self.rows(declaration)[0]["conclusion"]["code"], "convergence")

    def test_capacity_or_speaker_person_change_invalidates_qualification(self):
        declaration, _, _, _ = self.make_chain()
        declaration.capacity = "ministre fictive, nouvelle capacité documentée"
        declaration.full_clean()
        declaration.save()
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )
        other = Person.objects.create(
            stable_id=self.unique("PERSON-OTHER"),
            display_name="Autre élue fictive",
            institutional_ids={"test": self.unique("ID-OTHER")},
        )
        declaration, _, _, _ = self.make_chain()
        declaration.speaker_person = other
        declaration.speaker_label = other.display_name
        declaration.full_clean()
        declaration.save()
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )

    def test_year_month_interval_and_estimated_dates_are_unknown_chronology(self):
        precisions = (
            Declaration.DatePrecision.YEAR,
            Declaration.DatePrecision.MONTH,
            Declaration.DatePrecision.INTERVAL,
            Declaration.DatePrecision.ESTIMATED,
            Declaration.DatePrecision.UNKNOWN,
        )
        for precision in precisions:
            with self.subTest(precision=precision):
                declaration, measure, _, passage = self.make_chain()
                declaration.date_precision = precision
                declaration.full_clean()
                declaration.save()
                # FP-3 : la précision appartient à l'empreinte ; requalifier pour lire la chronologie.
                DeclarationMeasureLink(
                    declaration=declaration,
                    measure=measure,
                    revision=2,
                    qualification_status=DocumentaryQualification.QUALIFIED,
                    stance=DeclarationMeasureLink.Stance.SUPPORT,
                    evidence=passage,
                    rationale=f"Requalification avec précision {precision}.",
                ).save()
                row = self.rows(declaration)[0]
                self.assertEqual(row["chronology"]["code"], "unknown")
                self.assertNotEqual(row["chronology"]["code"], "before_vote")
                self.assertEqual(row["chronology"]["precision"], precision)

    def test_better_documented_day_precision_reevaluates_chronology(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.date_precision = Declaration.DatePrecision.YEAR
        declaration.full_clean()
        declaration.save()
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )
        DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            evidence=passage,
            rationale="Requalification avec précision annuelle.",
        ).save()
        self.assertEqual(self.rows(declaration)[0]["chronology"]["code"], "unknown")
        declaration.date_precision = Declaration.DatePrecision.DAY
        declaration.full_clean()
        declaration.save()
        self.assertFalse(
            any(row["conclusion"]["code"] in {"convergence", "tension"} for row in self.rows(declaration))
        )
        DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=3,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            evidence=passage,
            rationale="Requalification avec précision journalière mieux documentée.",
        ).save()
        self.assertEqual(self.rows(declaration)[0]["chronology"]["code"], "before_vote")

    def test_nous_keyword_does_not_force_collective_scope(self):
        declaration, _, _, _ = self.make_chain()
        self.assertIn("Nous", declaration.exact_quote)
        self.assertNotEqual(declaration.position_scope, Declaration.PositionScope.COLLECTIVE)
        row = self.rows(declaration)[0]
        self.assertEqual(row["actors"]["compared_actor"]["kind"], "person")
        self.assertEqual(row["conclusion"]["code"], "convergence")

    def test_collective_spokesperson_keeps_personal_declaration_distinct(self):
        declaration, measure, scrutiny, passage = self.make_chain()
        org = Organization.objects.create(
            stable_id=self.unique("GROUP"),
            name="Groupe fictif",
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
        collective_passage = EvidencePassage.objects.create(
            document=passage.document,
            exact_text="Au nom du groupe, nous voterons cette mesure.",
            locator="communiqué collectif",
            public_url="https://media.example/collectif/1",
        )
        collective = Declaration.objects.create(
            stable_id=self.unique("DECL-COLLECTIVE"),
            speaker_person=declaration.speaker_person,
            speaker_organization=org,
            speaker_label=declaration.speaker_label,
            capacity="porte-parole du groupe",
            exact_quote=collective_passage.exact_text,
            occurred_on=date(2023, 12, 1),
            position_scope=Declaration.PositionScope.COLLECTIVE,
            source_passage=collective_passage,
            medium=Declaration.Medium.PRESS,
            source_role=Declaration.SourceRole.ORIGINAL,
        )
        link = DeclarationMeasureLink(
            declaration=collective,
            measure=measure,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            rationale="Parole collective distincte, avec sa propre preuve.",
            evidence=collective_passage,
        )
        link.full_clean()
        link.save()
        personal = self.rows(declaration)[0]
        group = self.rows(collective)[0]
        self.assertEqual(personal["actors"]["compared_actor"]["kind"], "person")
        self.assertEqual(personal["conclusion"]["code"], "convergence")
        self.assertEqual(group["vote"]["state"], "not_personal")
        self.assertEqual(group["conclusion"]["code"], "not_comparable")
        self.assertEqual(group["actors"]["compared_actor"]["kind"], "organization")
        self.assertEqual(group["collective"]["state"], "group_distribution")
        self.assertGreaterEqual(group["collective"]["affiliations"], 1)
        self.assertIn("pas la position personnelle", group["collective"]["reserve"])

    def test_forbidden_vote_source_is_excluded(self):
        declaration, _, scrutiny, _ = self.make_chain()
        forbidden_document = self.make_document(source_id=self.unique("FORBIDDEN"))
        forbidden_document.source.rights_status = Source.RightsStatus.FORBIDDEN
        forbidden_document.source.save()
        observation = VoteObservation.objects.get(scrutiny=scrutiny, person=declaration.speaker_person)
        observation.source_document = forbidden_document
        observation.full_clean()
        observation.save()
        row = self.rows(declaration)[0]
        self.assertEqual(row["vote"]["state"], "missing")
        self.assertEqual(row["conclusion"]["code"], "inconclusive")
        self.assertTrue(row["vote"]["exclusions"])
        self.assertEqual(row["vote"]["exclusions"][0]["rights_status"], "forbidden")

    def test_filters_pagination_and_coverage_stats(self):
        declaration, measure, scrutiny, passage = self.make_chain()
        second_document = self.make_document(source_id=self.unique("SENAT"))
        second = self.make_scrutiny(second_document, institution="Sénat", legislature="2025-2026")
        second_measure = Measure.objects.create(
            stable_id=self.unique("MEASURE-2"),
            text_version=second.submitted_text,
            title="Mesure sénatoriale distincte",
            locator="article 2",
            plain_summary="Autre effet concret.",
            concrete_effect="Un autre plancher est modifié.",
        )
        DeclarationMeasureLink(
            declaration=declaration,
            measure=second_measure,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            rationale="Seconde relation qualifiée pour pagination.",
            evidence=passage,
        ).save()
        MeasureScrutinyLink(
            measure=second_measure,
            scrutiny=second,
            qualification_status=DocumentaryQualification.QUALIFIED,
            vote_effect=MeasureScrutinyLink.VoteEffect.ADVANCES,
            rationale="Scrutin sénatorial distinct.",
            evidence=EvidencePassage.objects.create(
                document=second_document,
                exact_text="Scrutin sénatorial.",
                locator="page",
                public_url=second.official_vote_url,
            ),
        ).save()
        VoteObservation.objects.create(
            scrutiny=second,
            person=declaration.speaker_person,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=second_document,
        )
        full = compare_declaration(declaration_id=declaration.pk, limit=1, offset=0)
        self.assertEqual(full["coverage_stats"]["matched"], 2)
        self.assertEqual(full["coverage_stats"]["returned"], 1)
        self.assertEqual(full["coverage_stats"]["limit"], 1)
        self.assertFalse(full["scan_complete"])
        page_two = compare_declaration(declaration_id=declaration.pk, limit=1, offset=1)
        self.assertEqual(len(page_two["rows"]), 1)
        self.assertNotEqual(full["rows"][0]["measure"]["stable_id"], page_two["rows"][0]["measure"]["stable_id"])
        senate = compare_declaration(declaration_id=declaration.pk, institution="Sénat")
        self.assertEqual(senate["coverage_stats"]["matched"], 1)
        self.assertEqual(senate["rows"][0]["scrutiny"]["institution"], "Sénat")
        empty = compare_declaration(declaration_id=declaration.pk, institution="Conseil constitutionnel")
        self.assertEqual(empty["rows"], [])
        reverse_rows = compare_measure(measure_id=measure.pk, legislature="16")["rows"]
        self.assertEqual(len(reverse_rows), 1)

    def test_three_proofs_remain_distinct_and_ui_exposes_coverage(self):
        declaration, _, _, _ = self.make_chain()
        row = self.rows(declaration)[0]
        self.assertNotEqual(row["proofs"]["citation"]["url"], row["proofs"]["text_version"]["url"])
        self.assertNotEqual(row["proofs"]["text_version"]["url"], row["proofs"]["scrutiny"]["url"])
        self.assertEqual(row["proofs"]["citation"]["kind"], "declaration_source")
        self.assertEqual(row["proofs"]["text_version"]["kind"], "official_text")
        self.assertEqual(row["proofs"]["scrutiny"]["kind"], "official_vote")
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.get(
            reverse("thematic_comparisons"),
            {"mode": "speech", "declaration": declaration.pk},
        )
        self.assertContains(response, "Filtres de couverture")
        self.assertContains(response, "Lecture courte")
        self.assertContains(response, "Preuves distinctes")
        self.assertContains(response, "Acteur comparé")
        self.assertContains(response, "Ce que cela change")
        self.assertContains(response, "Vérifier le scrutin officiel")
        self.assertContains(response, "Convergence documentée")
        self.assertContains(response, "Le site destinataire ne réinterprète pas")

    def test_preparation_records_stale_fingerprint_without_establishing_comparison(self):
        declaration, _, _, _ = self.make_chain()
        declaration.exact_quote = "Citation substantiellement modifiée sans nouvelle qualification."
        declaration.save()
        snapshot = thematic_preparation_snapshot(declaration)
        self.assertTrue(snapshot["stale_qualified_links"])
        self.assertFalse(snapshot["comparability_established"])
        self.assertFalse(snapshot["human_review_created"])
        case, _ = prepare_research_case("recherche enregistrée")
        prep, created = prepare_documentary_review(case, counter_queries=["contre éléments"])
        self.assertTrue(created)
        self.assertFalse(prep.payload["comparability_established"])
        self.assertIsNone(prep.payload["political_label"])
