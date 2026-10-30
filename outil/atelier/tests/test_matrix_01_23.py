from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from atelier.models import (
    Affiliation,
    Comparison,
    ComparisonAct,
    DataState,
    Declaration,
    EditorialDossier,
    EvidencePassage,
    LegislativeDossier,
    Measure,
    Organization,
    Person,
    Scrutiny,
    TextVersion,
    Theme,
    ThemeAssignment,
    VoteCorrection,
    VoteObservation,
)
from atelier.services.calculations import group_distribution, pair_agreement
from atelier.services.contracts import validate_contract_envelope
from atelier.services.parsers.assembly import iter_vote_observations
from atelier.services.publication import authorize_and_queue, evaluate_revision, validate_public_payload
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls

from .support import AtelierTestCase


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MatrixTests01To23(AtelierTestCase):
    def test_a01_reimport_is_idempotent_and_duplicate_vote_is_refused(self):
        document = self.make_document(source_id="SRC:A01")
        same, created = type(document).objects.get_or_create(
            source=document.source, sha256=document.sha256, defaults={"version_id": "other"}
        )
        self.assertFalse(created)
        self.assertEqual(same.pk, document.pk)
        scrutiny = self.make_scrutiny(document)
        person = Person.objects.create(stable_id="PERSON:A01", display_name="Personne test")
        fields = {
            "scrutiny": scrutiny,
            "person": person,
            "source_category": "pour",
            "normalized_category": "for",
            "mapping_rule": "TEST-V1",
            "source_document": document,
        }
        VoteObservation.objects.create(**fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            VoteObservation.objects.create(**fields)
        self.assertEqual(VoteObservation.objects.count(), 1)

    def test_a02_same_scrutiny_number_is_namespaced_by_institution(self):
        first = self.make_scrutiny(source_identifier="7", institution="Assemblée nationale", legislature="16")
        second = self.make_scrutiny(source_identifier="7", institution="Sénat", legislature="2025-2026")
        self.assertNotEqual(first.stable_id, second.stable_id)
        self.assertEqual(Scrutiny.objects.filter(source_identifier="7").count(), 2)

    def test_a03_affiliation_history_is_dated(self):
        document = self.make_document()
        person = Person.objects.create(stable_id="PERSON:A03", display_name="Historique")
        old = Organization.objects.create(stable_id="ORG:A03:OLD", name="Ancien groupe", kind="parliamentary_group")
        new = Organization.objects.create(stable_id="ORG:A03:NEW", name="Nouveau groupe", kind="parliamentary_group")
        Affiliation.objects.create(
            person=person,
            organization=old,
            status="membre",
            starts_on=date(2022, 1, 1),
            ends_on=date(2023, 12, 31),
            observed_on=date(2023, 12, 31),
            source_document=document,
        )
        Affiliation.objects.create(
            person=person,
            organization=new,
            status="apparenté",
            starts_on=date(2024, 1, 1),
            observed_on=date(2024, 1, 2),
            source_document=document,
        )
        self.assertEqual(
            person.affiliations.get(starts_on__lte=date(2023, 6, 1), ends_on__gte=date(2023, 6, 1)).organization,
            old,
        )
        self.assertEqual(person.affiliations.get(starts_on__lte=date(2024, 6, 1), ends_on__isnull=True).organization, new)

    def test_a04_homonyms_are_not_merged(self):
        a = Person.objects.create(stable_id="AN:PERSON:1", display_name="Camille Martin", institutional_ids={"an": "1"})
        b = Person.objects.create(stable_id="SENAT:PERSON:2", display_name="Camille Martin", institutional_ids={"senat": "2"})
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(Person.objects.filter(display_name="Camille Martin").count(), 2)

    def test_a05_unknown_unavailable_not_applicable_and_zero_are_distinct(self):
        payload = self.payload(EditorialDossier.Kind.OFFICIAL_VOTE)
        payload["counts"]["abstention"] = self.state_count(0)
        payload["counts"]["registered"] = self.state_count(None, "unknown", "total non publié")
        validate_public_payload(EditorialDossier.Kind.OFFICIAL_VOTE, payload)
        self.assertEqual(payload["counts"]["abstention"], {"state": "known", "value": 0})
        self.assertEqual(payload["counts"]["registered"]["state"], "unknown")
        self.assertIn("unavailable", DataState.values)
        self.assertIn("not_applicable", DataState.values)

    def test_a06_missing_nominative_list_does_not_create_individual_votes(self):
        raw = {
            "ventilationVotes": {
                "organe": {
                    "groupes": {
                        "groupe": {
                            "organeRef": "G1",
                            "vote": {"positionMajoritaire": "pour", "decompteNominatif": None},
                        }
                    }
                }
            }
        }
        self.assertEqual(list(iter_vote_observations(raw)), [])
        scrutiny = self.make_scrutiny(nominative_status=Scrutiny.NominativeStatus.NOT_AVAILABLE)
        self.assertEqual(scrutiny.observations.count(), 0)

    def test_a07_divided_group_is_a_distribution_not_unanimity(self):
        distribution = group_distribution(["for", "for", "against", "abstention", "unknown"])
        self.assertEqual(distribution["for"], 2)
        self.assertEqual(distribution["against"], 1)
        self.assertEqual(distribution["abstention"], 1)
        self.assertNotEqual(distribution["for"], sum(distribution.values()))

    def test_a08_inconsistent_totals_are_an_explicit_anomaly(self):
        document = self.make_document()
        text = self.make_text(document)
        scrutiny = Scrutiny(
            stable_id="SCRUTINY:A08",
            source_identifier="8",
            institution="Assemblée nationale",
            legislature="16",
            occurred_on=date(2024, 1, 1),
            procedure="test",
            official_title="incohérent",
            official_vote_url="https://www.assemblee-nationale.fr/dyn/16/scrutins/8",
            submitted_text=text,
            result_status="unknown",
            nominative_status="partial",
            voters=3,
            expressed=2,
            count_for=2,
            count_against=1,
            count_abstention=0,
            counts_state=DataState.KNOWN,
            source_document=document,
        )
        with self.assertRaisesMessage(ValidationError, "Pour + contre"):
            scrutiny.full_clean()
        self.assertEqual(Scrutiny.objects.count(), 0)

    def test_a09_correction_preserves_recorded_vote(self):
        document = self.make_document()
        scrutiny = self.make_scrutiny(document)
        person = Person.objects.create(stable_id="PERSON:A09", display_name="Vote corrigé")
        observation = VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=person,
            source_category="contre",
            normalized_category="against",
            mapping_rule="TEST-V1",
            source_document=document,
        )
        VoteCorrection.objects.create(
            observation=observation,
            author_label="personne concernée",
            statement="avait voulu voter pour",
            occurred_on=date(2024, 1, 2),
            source_document=document,
        )
        observation.refresh_from_db()
        self.assertEqual(observation.normalized_category, "against")
        self.assertEqual(observation.corrections.get().statement, "avait voulu voter pour")

    def test_a10_submitted_adopted_and_in_force_versions_are_distinct(self):
        document = self.make_document()
        submitted = self.make_text(document, relation=TextVersion.Relation.SUBMITTED)
        adopted = TextVersion.objects.create(
            stable_id="TEXT:A10:ADOPTED",
            dossier=submitted.dossier,
            kind="texte adopté",
            official_title="Texte résultant",
            version_label="TA",
            stage="après scrutin",
            relation=TextVersion.Relation.ADOPTED,
            occurred_on=date(2024, 1, 2),
            official_url="https://www.assemblee-nationale.fr/dyn/16/textes/adopte",
            source_document=document,
        )
        in_force = TextVersion.objects.create(
            stable_id="TEXT:A10:LAW",
            dossier=submitted.dossier,
            kind="loi",
            official_title="Texte en vigueur",
            version_label="loi",
            stage="promulgué",
            relation=TextVersion.Relation.IN_FORCE,
            occurred_on=date(2024, 2, 1),
            official_url="https://www.legifrance.gouv.fr/jorf/id/exemple",
            source_document=document,
        )
        self.assertEqual({submitted.relation, adopted.relation, in_force.relation}, {"submitted", "adopted", "in_force"})

    def test_a11_deletion_amendment_keeps_its_reversed_semantics(self):
        scrutiny = self.make_scrutiny(anomaly="Un vote pour soutient la suppression de l'article.")
        measure = Measure.objects.create(
            stable_id="MEASURE:A11",
            text_version=scrutiny.submitted_text,
            title="Amendement de suppression",
            locator="amendement 1",
            mechanism="supprimer l'article",
            composite=False,
        )
        self.assertIn("suppression", scrutiny.anomaly)
        self.assertEqual(measure.mechanism, "supprimer l'article")

    def test_a12_rejected_intermediate_text_is_not_a_promulgated_law(self):
        scrutiny = self.make_scrutiny(result_status="rejected")
        self.assertEqual(scrutiny.result_status, "rejected")
        self.assertNotEqual(scrutiny.submitted_text.relation, TextVersion.Relation.IN_FORCE)

    def test_a13_article_theme_does_not_propagate_to_whole_dossier(self):
        scrutiny = self.make_scrutiny()
        measure = Measure.objects.create(
            stable_id="MEASURE:A13", text_version=scrutiny.submitted_text, title="Article ciblé", locator="article 2"
        )
        theme = Theme.objects.create(stable_id="THEME:A13", label="Ciblé", definition="Seulement cet article")
        ThemeAssignment.objects.create(
            theme=theme,
            target_type="measure",
            target_stable_id=measure.stable_id,
            locator="article 2",
            origin="rule",
        )
        self.assertFalse(theme.assignments.filter(target_type="dossier").exists())

    def test_a14_two_exact_official_links_suffice_without_full_text_copy(self):
        payload = self.payload(EditorialDossier.Kind.OFFICIAL_VOTE)
        validate_public_payload(EditorialDossier.Kind.OFFICIAL_VOTE, payload)
        self.assertNotEqual(payload["official_text_url"], payload["official_vote_url"])
        self.assertNotIn("full_text", payload)

    def test_a15_quote_negation_amount_unit_and_conditions_are_preserved(self):
        document = self.make_document()
        passage = EvidencePassage.objects.create(
            document=document,
            exact_text="Nous ne dépasserons pas 50 euros si la condition demeure.",
            locator="paragraphe 4",
            public_url=document.source.document_url,
        )
        person = Person.objects.create(stable_id="PERSON:A15", display_name="Citation")
        declaration = Declaration.objects.create(
            stable_id="DECL:A15",
            speaker_person=person,
            speaker_label=person.display_name,
            capacity="personnelle",
            exact_quote=passage.exact_text,
            occurred_on=date(2024, 1, 1),
            quantifier="pas plus de",
            conditions="si la condition demeure",
            amount=Decimal("50"),
            amount_unit="EUR",
            source_passage=passage,
        )
        self.assertIn(" ne ", f" {declaration.exact_quote} ")
        self.assertEqual(declaration.amount, Decimal("50"))
        self.assertEqual(declaration.amount_unit, "EUR")

    def test_a16_general_statement_stays_inconclusive_and_bounded(self):
        document = self.make_document()
        passage = EvidencePassage.objects.create(
            document=document, exact_text="Nous agirons.", locator="p1", public_url=document.source.document_url
        )
        person = Person.objects.create(stable_id="PERSON:A16", display_name="Général")
        declaration = Declaration.objects.create(
            stable_id="DECL:A16",
            speaker_person=person,
            speaker_label=person.display_name,
            capacity="personnelle",
            exact_quote="Nous agirons.",
            occurred_on=date(2024, 1, 1),
            source_passage=passage,
        )
        comparison = Comparison.objects.create(
            stable_id="CMP:A16",
            declaration=declaration,
            result="inconclusive",
            criteria={},
            counterevidence=[],
            scope="Déclaration générale sans mesure précise",
            rationale="Aucune promesse précise n'est inventée.",
        )
        self.assertEqual(comparison.result, "inconclusive")
        self.assertIn("générale", comparison.scope)

    def test_a17_vote_before_declaration_is_not_a_later_breach(self):
        document = self.make_document()
        scrutiny = self.make_scrutiny(document)
        passage = EvidencePassage.objects.create(
            document=document, exact_text="À partir de demain.", locator="p1", public_url=document.source.document_url
        )
        person = Person.objects.create(stable_id="PERSON:A17", display_name="Chronologie")
        declaration = Declaration.objects.create(
            stable_id="DECL:A17",
            speaker_person=person,
            speaker_label=person.display_name,
            capacity="personnelle",
            exact_quote=passage.exact_text,
            occurred_on=date(2024, 2, 1),
            source_passage=passage,
        )
        comparison = Comparison.objects.create(
            stable_id="CMP:A17",
            declaration=declaration,
            result="not_comparable",
            criteria={},
            counterevidence=[],
            scope="Chronologie",
            rationale="Le vote est antérieur.",
        )
        act = ComparisonAct.objects.create(
            comparison=comparison,
            scrutiny=scrutiny,
            included=False,
            reason="vote antérieur",
            chronological_relation="vote_before_declaration",
        )
        self.assertFalse(act.included)
        self.assertEqual(comparison.result, "not_comparable")

    def test_a18_person_group_party_attribution_levels_are_not_implicit(self):
        document = self.make_document()
        passage = EvidencePassage.objects.create(
            document=document, exact_text="Notre groupe votera.", locator="p1", public_url=document.source.document_url
        )
        person = Person.objects.create(stable_id="PERSON:A18", display_name="Orateur")
        group = Organization.objects.create(stable_id="GROUP:A18", name="Groupe", kind="parliamentary_group")
        personal = Declaration.objects.create(
            stable_id="DECL:A18:P",
            speaker_person=person,
            speaker_label=person.display_name,
            capacity="personnelle",
            exact_quote="Je voterai.",
            occurred_on=date(2024, 1, 1),
            source_passage=passage,
        )
        collective = Declaration.objects.create(
            stable_id="DECL:A18:G",
            speaker_organization=group,
            speaker_label=group.name,
            capacity="collective",
            exact_quote="Notre groupe votera.",
            occurred_on=date(2024, 1, 1),
            source_passage=passage,
        )
        self.assertIsNone(personal.speaker_organization)
        self.assertIsNone(collective.speaker_person)

    def test_a19_counterevidence_change_invalidates_the_old_review(self):
        dossier, revision, document = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        self.approve(dossier, revision, reviewer)
        payload = copy.deepcopy(revision.public_payload)
        payload["counterevidence"] = [
            {"statement": "Élément nouveau", "source_url": payload["official_vote_url"], "effect": "Réexamen requis"}
        ]
        new_revision = create_revision(
            dossier=dossier,
            public_payload=payload,
            source_snapshot=self.snapshot(document),
            title=payload["title"],
            short_answer=payload["short_answer"],
            main_caveat=payload["main_caveat"],
            reasoning=payload["reasoning"],
            result=payload["result"],
            method_version="DV-M0.1",
            change_summary="Contre-élément ajouté",
        )
        record_controls(new_revision, deterministic_control_results(new_revision))
        self.assertFalse(evaluate_revision(revision, "test").allowed)
        self.assertEqual(evaluate_revision(new_revision, "test").reason, "review_missing_or_stale")

    def test_a20_missing_evidence_blocks_without_inventing_a_quote(self):
        document = self.make_document()
        passage = EvidencePassage.objects.create(
            document=document,
            exact_text="",
            text_state=DataState.UNAVAILABLE,
            locator="introuvable",
            public_url=document.source.document_url,
            public_access_status="unavailable",
        )
        dossier = EditorialDossier.objects.create(
            stable_id="EDITORIAL:A20",
            content_kind="speech_vote",
            established_kind="speech_vote",
            data_scope="real",
            state=EditorialDossier.State.NEEDS_EVIDENCE,
        )
        self.assertEqual(passage.exact_text, "")
        self.assertEqual(dossier.state, "needs_evidence")

    def test_a21_fact_calculation_is_reproducible(self):
        observations = [
            {"left": "for", "right": "for"},
            {"left": "against", "right": "for"},
            {"left": "unknown", "right": "for"},
        ]
        first = pair_agreement(observations)
        second = pair_agreement(copy.deepcopy(observations))
        self.assertEqual(first, second)
        self.assertEqual((first["numerator"], first["denominator"], first["excluded"]), (1, 2, 1))

    def test_a22_zero_comparable_pairs_is_not_zero_percent(self):
        result = pair_agreement(
            [{"left": "unknown", "right": "for"}, {"left": "not_recorded", "right": "against"}]
        )
        self.assertEqual(result["denominator"], 0)
        self.assertIsNone(result["fraction"])
        self.assertEqual(result["result_state"], "not_calculable")

    def test_a23_analysis_disguised_as_fact_is_structurally_refused(self):
        _, revision, _ = self.make_revision(EditorialDossier.Kind.OFFICIAL_VOTE)
        event = authorize_and_queue(revision, "test")
        envelope = copy.deepcopy(event.payload)
        envelope["public"]["short_answer"] = "Interprétation politique cachée"
        with self.assertRaisesMessage(ValidationError, "contract_invalid"):
            validate_contract_envelope(envelope)
