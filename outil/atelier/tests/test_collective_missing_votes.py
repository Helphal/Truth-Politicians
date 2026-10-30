from datetime import timedelta
from atelier.models import Affiliation, Person, Organization, VoteObservation, EvidencePassage, Declaration, Comparison, ComparisonAct
from atelier.services.thematic_integrity import collective_vote_distribution, collective_distribution_contract
from atelier.services.catalogue import prepare_public_snapshot
from .support import AtelierTestCase


class CollectiveMissingVotesTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self.doc=self.make_document();self.scrutiny=self.make_scrutiny(self.doc)
        self.group=Organization.objects.create(stable_id='SYNTH:GROUP',name='SYNTH group',kind='parliamentary_group')
        self.people=[]
        for i in range(20):
            person=Person.objects.create(stable_id=f'SYNTH:PERSON:{i}',display_name=f'SYNTH person {i}')
            self.people.append(person)
            Affiliation.objects.create(person=person,organization=self.group,status='SYNTH-dated',
                starts_on=self.scrutiny.occurred_on,observed_on=self.scrutiny.occurred_on,source_document=self.doc)
            if i<17:
                category='for' if i<14 else 'against'
                VoteObservation.objects.create(person=person,scrutiny=self.scrutiny,affiliation_at_event=self.group,
                    source_category=category,normalized_category=category,mapping_rule='SYNTH',source_document=self.doc)

    def test_twenty_members_and_seventeen_voices_keep_three_unknown(self):
        material=collective_vote_distribution(self.group,self.scrutiny)
        self.assertEqual(material['affiliations'],20)
        self.assertEqual(material['usable_voices'],17)
        self.assertEqual(material['distribution'],{'for':14,'against':3})
        contract=collective_distribution_contract(material)
        self.assertEqual(contract['state'],'partial')
        self.assertEqual(contract['unknown_member_count'],3)
        self.assertEqual(set(contract['excluded_member_ids']),{p.stable_id for p in self.people[17:]})
        self.assertEqual(VoteObservation.objects.count(),17)

    def test_projected_catalogue_keeps_members_without_creating_votes(self):
        passage=EvidencePassage.objects.create(document=self.doc,exact_text='SYNTH declaration.',locator='p1',public_url=self.doc.source.document_url)
        declaration=Declaration.objects.create(stable_id='DECL:SYNTH',speaker_person=self.people[0],speaker_organization=self.group,
            speaker_label=self.people[0].display_name,capacity='SYNTH spokesperson',exact_quote=passage.exact_text,
            occurred_on=self.scrutiny.occurred_on,source_passage=passage,position_scope='collective')
        comparison=Comparison.objects.create(stable_id='SYNTH:COMPARISON',declaration=declaration,result='inconclusive')
        ComparisonAct.objects.create(comparison=comparison,scrutiny=self.scrutiny,chronological_relation='same_day')
        future=Person.objects.create(stable_id='SYNTH:FUTURE',display_name='SYNTH future member')
        Affiliation.objects.create(person=future,organization=self.group,status='SYNTH-dated',starts_on=self.scrutiny.occurred_on+timedelta(days=1),
            observed_on=self.scrutiny.occurred_on,source_document=self.doc)
        payload,_=prepare_public_snapshot('speech_vote',{},self.snapshot(self.doc),comparison=comparison)
        self.assertEqual(len(payload['catalogue']['persons']),20)
        self.assertEqual(len(payload['catalogue']['vote_observations']),17)
        self.assertNotIn(future.stable_id,{x['id'] for x in payload['catalogue']['persons']})
        self.assertTrue(set(payload['collective_distribution']['excluded_member_ids'])<={x['id'] for x in payload['catalogue']['persons']})
