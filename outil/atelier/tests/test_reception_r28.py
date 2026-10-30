"""Contre-revue R28 : base temporaire, acteurs fictifs, aucun réseau ni revue réelle."""
from django.test import override_settings
from atelier.models import (DeclarationMeasureLink, DocumentaryQualification, Organization,
                            Scrutiny, VoteObservation, DataState)
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_thematic_comparisons as fixtures
from atelier.services.thematic_comparisons import compare_declaration, compare_measure


@override_settings(DV_ENV='test', DV_NETWORK_MODE='disabled')
class R28RegressionTests(AtelierTestCase):
    make_chain = fixtures.ThematicComparisonTests.make_chain

    def rows(self, declaration):
        return compare_declaration(declaration_id=declaration.pk)['rows']

    def assert_not_directional(self, declaration):
        rows = self.rows(declaration)
        self.assertFalse(any(r['conclusion']['code'] in ('convergence', 'tension') for r in rows),
                         'Une relation périmée ou une attribution non établie conserve une conclusion directionnelle.')

    def test_T01_valid_chain_same_in_both_directions(self):
        declaration, measure, _, _ = self.make_chain()
        self.assertEqual(self.rows(declaration)[0]['conclusion']['code'], 'convergence')
        self.assertEqual(compare_measure(measure_id=measure.pk)['rows'][0]['declaration']['stable_id'], declaration.stable_id)

    def test_T02_changed_quote_requires_new_qualification(self):
        declaration, _, _, _ = self.make_chain()
        declaration.exact_quote = 'Je refuse le relèvement du salaire minimum proposé dans ce texte.'
        declaration.paraphrase = 'Opposition explicite à la hausse.'
        declaration.full_clean(); declaration.save()
        self.assert_not_directional(declaration)

    def test_T03_changed_measure_effect_requires_new_qualification(self):
        declaration, measure, _, _ = self.make_chain()
        measure.plain_summary = 'Le texte abaisse le salaire minimum légal.'
        measure.concrete_effect = 'Le plancher salarial brut est diminué.'
        measure.full_clean(); measure.save()
        self.assert_not_directional(declaration)

    def test_T04_quarantined_vote_version_cannot_support_conclusion(self):
        declaration, _, scrutiny, _ = self.make_chain()
        excluded = self.make_document(source_id=self.unique('VOTE_QUARANTINE'))
        excluded.quarantined = True; excluded.save()
        observation = VoteObservation.objects.get(scrutiny=scrutiny, person=declaration.speaker_person)
        observation.source_document = excluded
        observation.full_clean(); observation.save()
        self.assert_not_directional(declaration)

    def test_T05_spokesperson_is_not_automatically_personal_position(self):
        declaration, measure, _, passage = self.make_chain()
        org = Organization.objects.create(stable_id=self.unique('GROUP'), name='Groupe fictif', kind=Organization.Kind.PARLIAMENTARY_GROUP)
        declaration.speaker_organization = org
        declaration.capacity = 'Porte-parole du groupe fictif, parlant au nom du groupe'
        declaration.quantifier = 'Position collective du groupe, non engagement personnel du porte-parole'
        declaration.full_clean(); declaration.save()
        # A new qualification after the attribution change isolates the scope issue
        # from the separate problem of stale qualifications.
        fresh = DeclarationMeasureLink(declaration=declaration, measure=measure, revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT, evidence=passage,
            rationale='Position collective du groupe expressément qualifiée, sans engagement personnel.')
        fresh.full_clean(); fresh.save()
        self.assert_not_directional(declaration)

    def test_T06_non_nominative_scrutiny_with_inconsistent_row_is_not_attributable(self):
        declaration, _, scrutiny, _ = self.make_chain()
        scrutiny.nominative_status = Scrutiny.NominativeStatus.NOT_AVAILABLE
        scrutiny.full_clean(); scrutiny.save()
        self.assert_not_directional(declaration)

    def test_T07_unknown_date_state_is_not_proven_chronology(self):
        declaration, measure, _, passage = self.make_chain()
        declaration.occurred_on_state = DataState.UNKNOWN
        declaration.full_clean(); declaration.save()
        # FP-3 : l'état de date fait partie de l'empreinte → qualification périmée.
        self.assert_not_directional(declaration)
        fresh = DeclarationMeasureLink(
            declaration=declaration, measure=measure, revision=2,
            qualification_status=DocumentaryQualification.QUALIFIED,
            stance=DeclarationMeasureLink.Stance.SUPPORT, evidence=passage,
            rationale='Requalification explicite avec état de date inconnu.')
        fresh.full_clean(); fresh.save()
        self.assertEqual(self.rows(declaration)[0]['chronology']['code'], 'unknown')

    def test_T08_explicit_new_candidate_hides_older_qualification(self):
        declaration, measure, _, passage = self.make_chain()
        link = DeclarationMeasureLink(declaration=declaration, measure=measure, revision=2,
            qualification_status=DocumentaryQualification.CANDIDATE,
            stance=DeclarationMeasureLink.Stance.UNCLEAR, evidence=passage,
            rationale='Nouvelle qualification explicitement en attente.')
        link.full_clean(); link.save()
        self.assertEqual(self.rows(declaration), [])
