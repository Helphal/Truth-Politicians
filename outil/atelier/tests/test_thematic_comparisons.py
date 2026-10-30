from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse

from atelier.models import (
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    EvidencePassage,
    HumanReview,
    Measure,
    MeasureScrutinyLink,
    Organization,
    OutboxEvent,
    Person,
    VoteObservation,
    UserProfile,
)
from atelier.services.thematic_comparisons import (
    compare_declaration,
    compare_measure,
    search_declarations,
    search_measures,
)
from atelier.tests.support import AtelierTestCase


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class ThematicComparisonTests(AtelierTestCase):
    def make_chain(
        self,
        *,
        stance=DeclarationMeasureLink.Stance.SUPPORT,
        vote_effect=MeasureScrutinyLink.VoteEffect.ADVANCES,
        category=VoteObservation.Category.FOR,
        declaration_date=date(2023, 12, 1),
        person=True,
    ):
        declaration_document = self.make_document(source_id=self.unique("PRESS"))
        declaration_document.source.producer = "Média public de test"
        declaration_document.source.family = "press"
        declaration_document.source.save(update_fields=["producer", "family", "updated_at"])
        quote = "Nous défendrons une hausse durable des rémunérations les plus basses."
        passage = EvidencePassage.objects.create(
            document=declaration_document,
            exact_text=quote,
            locator="entretien, 01:24",
            context="Pouvoir d'achat et rémunérations.",
            public_url="https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/racc001-corpus-press",
        )
        speaker_person = None
        speaker_organization = None
        if person:
            speaker_person = Person.objects.create(
                stable_id=self.unique("PERSON"),
                display_name="Élue fictive",
                institutional_ids={"test": self.unique("ID")},
            )
        else:
            speaker_organization = Organization.objects.create(
                stable_id=self.unique("ORG"),
                name="Organisation fictive",
                kind=Organization.Kind.PARTY,
            )
        declaration = Declaration.objects.create(
            stable_id=self.unique("DECLARATION"),
            speaker_person=speaker_person,
            speaker_organization=speaker_organization,
            speaker_label=speaker_person.display_name if speaker_person else speaker_organization.name,
            capacity="députée fictive" if person else "organisation fictive",
            exact_quote=quote,
            paraphrase="Défendre une hausse des bas salaires.",
            occurred_on=declaration_date,
            source_passage=passage,
            medium=Declaration.Medium.PRESS,
            source_role=Declaration.SourceRole.ORIGINAL,
        )
        scrutiny_document = self.make_document(source_id=self.unique("OFFICIAL"))
        scrutiny = self.make_scrutiny(scrutiny_document)
        measure = Measure.objects.create(
            stable_id=self.unique("MEASURE"),
            text_version=scrutiny.submitted_text,
            title="Revalorisation du salaire minimum",
            locator="article 1",
            mechanism="Relèvement du montant légal.",
            plain_summary="Le texte augmente le salaire minimum légal.",
            concrete_effect="Le plancher salarial brut est relevé.",
        )
        declaration_link = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=stance,
            rationale="La citation vise explicitement la hausse des rémunérations les plus basses.",
            evidence=passage,
        )
        declaration_link.full_clean()
        declaration_link.save()
        scrutiny_evidence = EvidencePassage.objects.create(
            document=scrutiny_document,
            exact_text="L'article 1 est soumis au vote.",
            locator="scrutin et texte soumis",
            public_url=scrutiny.official_vote_url,
        )
        scrutiny_link = MeasureScrutinyLink(
            measure=measure,
            scrutiny=scrutiny,
            qualification_status=DocumentaryQualification.QUALIFIED,
            vote_effect=vote_effect,
            rationale="Le scrutin porte sur l'adoption de la mesure.",
            evidence=scrutiny_evidence,
        )
        scrutiny_link.full_clean()
        scrutiny_link.save()
        if person and category is not None:
            VoteObservation.objects.create(
                scrutiny=scrutiny,
                person=speaker_person,
                source_category=dict(VoteObservation.Category.choices)[category],
                normalized_category=category,
                mapping_rule="TEST-EXACT",
                source_document=scrutiny_document,
            )
        return declaration, measure, scrutiny, passage

    def test_support_for_advancing_vote_is_convergence(self):
        declaration, _, _, _ = self.make_chain()
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["conclusion"]["code"], "convergence")
        self.assertEqual(row["vote"]["category"], VoteObservation.Category.FOR)
        self.assertEqual(row["chronology"]["code"], "before_vote")

    def test_support_against_advancing_vote_is_tension(self):
        declaration, _, _, _ = self.make_chain(category=VoteObservation.Category.AGAINST)
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["conclusion"]["code"], "tension")

    def test_opposition_and_blocking_effect_are_inverted_correctly(self):
        declaration, _, _, _ = self.make_chain(
            stance=DeclarationMeasureLink.Stance.OPPOSE,
            vote_effect=MeasureScrutinyLink.VoteEffect.BLOCKS,
            category=VoteObservation.Category.FOR,
        )
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["conclusion"]["code"], "convergence")

    def test_abstention_non_participation_and_unknown_are_never_opposition(self):
        categories = (
            VoteObservation.Category.ABSTENTION,
            VoteObservation.Category.NON_VOTING,
            VoteObservation.Category.NOT_RECORDED,
            VoteObservation.Category.UNKNOWN,
        )
        for category in categories:
            with self.subTest(category=category):
                declaration, _, _, _ = self.make_chain(category=category)
                row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
                self.assertEqual(row["conclusion"]["code"], "not_comparable")
                self.assertIn("ne sont pas transformés en opposition", row["conclusion"]["explanation"])

    def test_missing_vote_does_not_invent_abstention(self):
        declaration, _, _, _ = self.make_chain(category=None)
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["vote"]["state"], "missing")
        self.assertEqual(row["conclusion"]["code"], "inconclusive")

    def test_collective_statement_is_not_assigned_to_a_person(self):
        declaration, _, _, _ = self.make_chain(person=False, category=None)
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["vote"]["state"], "not_personal")
        self.assertEqual(row["conclusion"]["code"], "not_comparable")

    def test_after_vote_is_never_worded_as_a_broken_promise(self):
        declaration, _, _, _ = self.make_chain(declaration_date=date(2024, 2, 1))
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["chronology"]["code"], "after_vote")
        self.assertIn("pas de la preuve d'une promesse rompue", row["conclusion"]["explanation"])

    def test_latest_candidate_revision_withholds_older_qualified_relation(self):
        declaration, measure, _, passage = self.make_chain()
        candidate = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.CANDIDATE,
            stance=DeclarationMeasureLink.Stance.UNCLEAR,
            rationale="La portée doit être réexaminée.",
            evidence=passage,
        )
        candidate.full_clean()
        candidate.save()
        result = compare_declaration(declaration_id=declaration.pk)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["qualified_measure_count"], 0)

    def test_conflicting_source_versions_are_exposed_not_silently_selected(self):
        declaration, _, scrutiny, _ = self.make_chain()
        second_document = self.make_document(source= scrutiny.source_document.source, captured_offset=10)
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=declaration.speaker_person,
            source_category="Contre",
            normalized_category=VoteObservation.Category.AGAINST,
            mapping_rule="TEST-EXACT",
            source_document=second_document,
        )
        row = compare_declaration(declaration_id=declaration.pk)["rows"][0]
        self.assertEqual(row["vote"]["state"], "conflict")
        self.assertEqual(row["conclusion"]["code"], "inconclusive")

    def test_reverse_search_starts_from_measure_and_returns_public_position(self):
        declaration, measure, _, _ = self.make_chain()
        result = compare_measure(measure_id=measure.pk)
        self.assertEqual(result["direction"], "law_to_public_positions")
        self.assertEqual(result["rows"][0]["declaration"]["stable_id"], declaration.stable_id)
        self.assertEqual(result["subject"]["concrete_effect"], "Le plancher salarial brut est relevé.")

    def test_search_covers_press_source_and_plain_language_measure(self):
        declaration, measure, _, _ = self.make_chain()
        speech = search_declarations(query="rémunérations")
        laws = search_measures(query="plancher salarial")
        self.assertEqual(speech["items"][0]["source"]["medium"], Declaration.Medium.PRESS)
        self.assertEqual(speech["items"][0]["source"]["source_role"], Declaration.SourceRole.ORIGINAL)
        self.assertEqual(laws["items"][0]["stable_id"], measure.stable_id)
        self.assertEqual(speech["items"][0]["stable_id"], declaration.stable_id)
        self.assertIn("pagination", speech)
        self.assertIn("coverage", laws)

    def test_no_network_review_or_publication_side_effect(self):
        declaration, measure, _, _ = self.make_chain()
        with patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")):
            speech = compare_declaration(declaration_id=declaration.pk)
            law = compare_measure(measure_id=measure.pk)
        self.assertFalse(speech["human_review_created"])
        self.assertFalse(law["publication_created"])
        self.assertEqual(HumanReview.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_qualified_links_require_explicit_direction_and_are_immutable(self):
        declaration, measure, _, passage = self.make_chain()
        invalid = DeclarationMeasureLink(
            declaration=declaration,
            measure=measure,
            revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.UNCLEAR,
            rationale="À préciser.",
            evidence=passage,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        existing = declaration.measure_links.get(revision=1)
        existing.rationale = "Modification interdite."
        with self.assertRaisesMessage(ValidationError, "declaration_measure_link_immutable"):
            existing.save()


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class ThematicComparisonRenderTests(AtelierTestCase):
    make_chain = ThematicComparisonTests.make_chain

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_page_explains_both_directions_and_renders_complete_chain(self):
        declaration, _, _, _ = self.make_chain()
        response = self.client.get(reverse("thematic_comparisons"), {"mode": "speech", "declaration": declaration.pk})
        self.assertContains(response, "De la parole publique aux actes")
        self.assertContains(response, "Partir d’une parole")
        self.assertContains(response, "Partir d’une loi")
        self.assertContains(response, "Ce que cela change")
        self.assertContains(response, "Vérifier le scrutin officiel")
        self.assertContains(response, "Convergence documentée")
        self.assertContains(response, 'aria-current="page"')

    def test_page_requires_authentication_and_does_not_offer_mutation(self):
        self.client.logout()
        response = self.client.get(reverse("thematic_comparisons"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(reverse("thematic_comparisons"))
        self.assertContains(response, '<form method="get" class="explorer-search"')
        self.assertNotContains(response, "/action/")
        self.assertContains(response, "Aucune conclusion automatique publiée")

    def test_qualification_action_creates_audited_revision_not_editorial_review(self):
        declaration, measure, _, passage = self.make_chain()
        response = self.client.post(
            reverse("thematic_qualification"),
            {
                "kind": "position",
                "position-declaration": declaration.pk,
                "position-measure": measure.pk,
                "position-stance": DeclarationMeasureLink.Stance.SUPPORT,
                "position-qualification_status": DocumentaryQualification.QUALIFIED,
                "position-evidence": passage.pk,
                "position-rationale": "Nouvelle vérification humaine de la relation documentaire.",
            },
        )
        self.assertRedirects(response, reverse("thematic_qualification"))
        revision = DeclarationMeasureLink.objects.get(declaration=declaration, measure=measure, revision=2)
        self.assertEqual(revision.recorded_by, self.user)
        self.assertEqual(HumanReview.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_collector_can_read_comparator_but_cannot_qualify(self):
        collector = self.make_user(role=UserProfile.Role.COLLECTOR)
        self.client.force_login(collector)
        self.assertEqual(self.client.get(reverse("thematic_comparisons")).status_code, 200)
        self.assertEqual(self.client.get(reverse("thematic_qualification")).status_code, 403)

    def test_qualification_form_rejects_unrelated_evidence_without_server_error(self):
        declaration, measure, _, _ = self.make_chain()
        unrelated = EvidencePassage.objects.create(
            document=self.make_document(),
            exact_text="Passage sans lien avec la déclaration.",
            locator="hors sujet",
            public_url="https://example.org/unrelated",
        )
        response = self.client.post(
            reverse("thematic_qualification"),
            {
                "kind": "position",
                "position-declaration": declaration.pk,
                "position-measure": measure.pk,
                "position-stance": DeclarationMeasureLink.Stance.SUPPORT,
                "position-qualification_status": DocumentaryQualification.QUALIFIED,
                "position-evidence": unrelated.pk,
                "position-rationale": "Tentative volontairement incohérente pour le contrôle.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choisissez le passage source exact")
        self.assertFalse(DeclarationMeasureLink.objects.filter(declaration=declaration, revision=2).exists())
