"""Couverture SUITE-005 au-delà de T01–T08 : reprise, dates, filtres, UI."""
from datetime import date

from django.test import override_settings
from django.urls import reverse

from atelier.models import (
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    EvidencePassage,
    Measure,
    MeasureScrutinyLink,
    VoteObservation,
)
from atelier.services.thematic_comparisons import compare_declaration, search_declarations
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_thematic_comparisons import ThematicComparisonTests


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Suite005CoverageTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def test_explicit_requalification_after_quote_change_restores_comparison(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.exact_quote = "Je refuse le relèvement du salaire minimum proposé dans ce texte."
        declaration.paraphrase = "Opposition explicite."
        declaration.full_clean()
        declaration.save()
        self.assertEqual(compare_declaration(declaration_id=declaration.pk)["rows"], [])

        passage.exact_text = declaration.exact_quote
        passage.save(update_fields=["exact_text", "updated_at"])
        revised = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.OPPOSE,
            rationale="Nouvelle qualification après correction de la citation exacte.",
            evidence=passage,
        )
        revised.full_clean()
        revised.save()
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["conclusion"]["code"], "tension")
        self.assertEqual(row["measure"]["position"], DeclarationMeasureLink.Stance.OPPOSE)

    def test_year_precision_is_not_proven_day_chronology(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.date_precision = Declaration.DatePrecision.YEAR
        declaration.full_clean()
        declaration.save()
        DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            evidence=passage,
            rationale="Requalification FP-3 avec précision annuelle.",
        ).save()
        self.assertEqual(
            compare_declaration(declaration_id=declaration.pk)["rows"][0]["chronology"]["code"],
            "unknown",
        )

    def test_search_and_compare_expose_filters_pagination_and_exclusions(self):
        declaration, _, scrutiny, _ = self.make_chain()
        search = search_declarations(query="rémunérations", limit=1, offset=0)
        self.assertEqual(search["pagination"]["returned"], 1)
        self.assertIn("coverage", search)

        excluded = self.make_document(source_id=self.unique("VOTE_QUARANTINE"))
        excluded.quarantined = True
        excluded.save()
        observation = VoteObservation.objects.get(scrutiny=scrutiny, person=declaration.speaker_person)
        observation.source_document = excluded
        observation.full_clean()
        observation.save()
        result = compare_declaration(declaration_id=declaration.pk, conclusion="inconclusive")
        self.assertEqual(result["filters"]["conclusion"], "inconclusive")
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["rows"][0]["vote"]["exclusions"][0]["reason"], "source_unusable")


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Suite005InterfaceTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_filters_pagination_and_proof_details_are_visible(self):
        declaration, _, _, _ = self.make_chain()
        response = self.client.get(
            reverse("thematic_comparisons"),
            {"mode": "speech", "declaration": declaration.pk, "conclusion": "convergence"},
        )
        self.assertContains(response, "Filtres de lecture")
        self.assertContains(response, "Preuves et source")
        self.assertContains(response, "Empreinte relation")
        self.assertContains(response, "Convergence documentée")
        self.assertContains(response, "Périmètre")
        self.assertContains(response, 'id="result-from"')
        self.assertContains(response, 'id="result-to"')
        search = self.client.get(
            reverse("thematic_comparisons"),
            {"mode": "speech", "q": "rémunérations", "from": "2023-01-01", "to": "2023-12-31"},
        )
        self.assertContains(search, "Filtres visibles")
        self.assertContains(search, "Candidats locaux seulement")

    def test_pagination_preserves_legislature_nominative_and_period_filters(self):
        declaration, _, _, passage = self.make_chain()
        second_document = self.make_document(source_id=self.unique("SENAT-PAGE"))
        second = self.make_scrutiny(second_document, institution="Sénat", legislature="2025-2026")
        second_measure = Measure.objects.create(
            stable_id=self.unique("MEASURE-PAGE"),
            text_version=second.submitted_text,
            title="Mesure pour pagination UI",
            locator="article page",
            plain_summary="Autre effet.",
            concrete_effect="Autre plancher.",
        )
        DeclarationMeasureLink(
            declaration=declaration,
            measure=second_measure,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            rationale="Seconde relation pour pagination UI.",
            evidence=passage,
        ).save()
        MeasureScrutinyLink(
            measure=second_measure,
            scrutiny=second,
            qualification_status=DocumentaryQualification.QUALIFIED,
            vote_effect=MeasureScrutinyLink.VoteEffect.ADVANCES,
            rationale="Scrutin distinct pour pagination UI.",
            evidence=EvidencePassage.objects.create(
                document=second_document,
                exact_text="Scrutin sénatorial paginé.",
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
        page = compare_declaration(declaration_id=declaration.pk, limit=1, offset=0)
        self.assertEqual(page["pagination"]["total"], 2)
        self.assertTrue(page["pagination"]["has_more"])

        filtered = self.client.get(
            reverse("thematic_comparisons"),
            {
                "mode": "speech",
                "declaration": declaration.pk,
                "legislature": "16",
                "nominative": "available",
                "from": "2023-01-01",
                "to": "2024-12-31",
                "limit": 1,
            },
        )
        self.assertContains(filtered, 'id="result-legislature"')
        self.assertContains(filtered, 'value="16"')
        self.assertContains(filtered, 'name="nominative"')
        self.assertContains(filtered, 'value="2023-01-01"')
        self.assertContains(filtered, 'value="2024-12-31"')

        paged = self.client.get(
            reverse("thematic_comparisons"),
            {
                "mode": "speech",
                "declaration": declaration.pk,
                "legislature": "16",
                "nominative": "available",
                "from": "2023-01-01",
                "to": "2024-12-31",
                "limit": 1,
                "offset": 0,
            },
        )
        # Deux scrutins existent, mais le filtre législature=16 n'en garde qu'un :
        # vérifier la conservation des filtres sur une page multi-résultats sans filtre institutionnel.
        multi = self.client.get(
            reverse("thematic_comparisons"),
            {
                "mode": "speech",
                "declaration": declaration.pk,
                "nominative": "available",
                "from": "2023-01-01",
                "to": "2026-12-31",
                "limit": 1,
            },
        )
        self.assertContains(multi, "Suivant")
        self.assertContains(multi, "nominative=available")
        self.assertContains(multi, "from=2023-01-01")
        self.assertContains(multi, "to=2026-12-31")
        self.assertContains(multi, "limit=1")

        declaration.exact_quote = "Citation périmée pour exclusion visible."
        declaration.save()
        stale = self.client.get(
            reverse("thematic_comparisons"),
            {"mode": "speech", "declaration": declaration.pk},
        )
        self.assertContains(stale, "Exclusions de relations")
        self.assertContains(stale, "stale_fingerprint")

    def test_search_pager_preserves_limit(self):
        for index in range(3):
            self.make_chain()
        response = self.client.get(
            reverse("thematic_comparisons"),
            {"mode": "speech", "q": "rémunérations", "limit": 1},
        )
        self.assertContains(response, "Suivant")
        self.assertContains(response, "limit=1")
        self.assertContains(response, "offset=1")
