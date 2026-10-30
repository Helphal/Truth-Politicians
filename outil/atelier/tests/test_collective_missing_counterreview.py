from copy import deepcopy
from atelier.tests import test_collective_missing_votes as fixtures
from atelier.models import VoteObservation
from atelier.services.thematic_integrity import collective_vote_distribution, collective_distribution_contract

class IndependentMissingCountertests(fixtures.CollectiveMissingVotesTests):
    def test_duplicate_exclusions_are_not_two_unknown_people(self):
        material=collective_vote_distribution(self.group,self.scrutiny)
        material['exclusions'].extend(deepcopy(material['exclusions']))
        result=collective_distribution_contract(material)
        self.assertEqual(result['unknown_member_count'],3)
        self.assertEqual(result['state'],'partial')

    def test_excluded_duplicate_does_not_erase_a_usable_voice(self):
        material=collective_vote_distribution(self.group,self.scrutiny)
        material['exclusions'].append({'reason':'source_unusable','person_id':self.people[0].pk})
        result=collective_distribution_contract(material)
        self.assertEqual(result['unknown_member_count'],3)
        self.assertIn(self.people[0].stable_id,result['included_member_ids'])

    def test_conflicting_category_adds_one_unknown_never_two_voices(self):
        conflicting_document=self.make_document()
        VoteObservation.objects.create(person=self.people[0],scrutiny=self.scrutiny,
            source_category='SYNTH conflict',normalized_category='against',mapping_rule='SYNTH',source_document=conflicting_document)
        material=collective_vote_distribution(self.group,self.scrutiny)
        self.assertEqual(material['usable_voices'],16)
        self.assertEqual(material['distribution'],{'for':13,'against':3})
        self.assertEqual(collective_distribution_contract(material)['unknown_member_count'],4)

    def test_all_known_preserves_existing_complete_contract(self):
        for person in self.people[17:]:
            VoteObservation.objects.create(person=person,scrutiny=self.scrutiny,
                source_category='SYNTH known',normalized_category='for',mapping_rule='SYNTH',source_document=self.doc)
        result=collective_distribution_contract(collective_vote_distribution(self.group,self.scrutiny))
        self.assertEqual(result['state'],'available')
        self.assertIsNone(result['unknown_member_count'])

    def test_nonnominative_never_exposes_a_personal_distribution(self):
        self.scrutiny.nominative_status='not_available'
        result=collective_distribution_contract(collective_vote_distribution(self.group,self.scrutiny))
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['included_member_ids'],[])

