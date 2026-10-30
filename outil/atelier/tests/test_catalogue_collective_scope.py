from copy import deepcopy
from datetime import timedelta
from django.core.exceptions import ValidationError
from atelier.models import Affiliation, Person, VoteObservation, Organization, EvidencePassage, Declaration, Comparison, ComparisonAct, EditorialDossier, EditorialRevision
from atelier.services.catalogue import build_catalogue, prepare_public_snapshot, binding_check
from atelier.services.contracts import validate_speech_vote_payload
from .test_collective_missing_votes import CollectiveMissingVotesTests


class CollectiveScopeTests(CollectiveMissingVotesTests):
    def selected(self,**kwargs):
        return build_catalogue([self.scrutiny.stable_id],collective_group=self.group,**kwargs)[0]

    def test_scope_preserves_missing_members_and_national_counts(self):
        stranger=Person.objects.create(stable_id='SYNTH:OTHER',display_name='SYNTH other')
        VoteObservation.objects.create(person=stranger,scrutiny=self.scrutiny,source_document=self.doc,
            source_category='for',normalized_category='for',mapping_rule='SYNTH')
        result=self.selected()
        self.assertEqual(len(result['persons']),20)
        self.assertEqual(len(result['selection']['scope_person_ids']),20)
        self.assertEqual(len(result['vote_observations']),17)
        self.assertEqual(VoteObservation.objects.count(),18)
        self.assertEqual(result['scrutinies'][0]['counts']['for']['value'],self.scrutiny.count_for)

    def test_future_expired_and_duplicate_affiliations(self):
        Affiliation.objects.create(person=self.people[0],organization=self.group,status='duplicate',starts_on=self.scrutiny.occurred_on,
            observed_on=None,source_document=self.doc)
        for suffix,start,end in [('future',1,None),('expired',-2,-1)]:
            p=Person.objects.create(stable_id='SYNTH:'+suffix,display_name=suffix)
            Affiliation.objects.create(person=p,organization=self.group,status='SYNTH',starts_on=self.scrutiny.occurred_on+timedelta(days=start),
                ends_on=self.scrutiny.occurred_on+timedelta(days=end) if end else None,source_document=self.doc)
        self.assertEqual(len(self.selected()['selection']['scope_person_ids']),20)

    def test_other_group_affiliations_not_projected_as_current(self):
        other=Organization.objects.create(stable_id='SYNTH:OLDGROUP',name='SYNTH old',kind='parliamentary_group')
        Affiliation.objects.create(person=self.people[0],organization=other,status='historical',
            starts_on=self.scrutiny.occurred_on-timedelta(days=100),source_document=self.doc)
        self.assertEqual({a['organization_id'] for a in self.selected()['affiliations']},{self.group.stable_id})
        self.assertTrue(Affiliation.objects.filter(organization=other).exists())

    def test_limit_is_refusal_not_truncation(self):
        with self.assertRaisesMessage(ValidationError,'members_missing_or_excessive'):
            self.selected(limit=19)
        self.assertEqual(len(self.selected(limit=20)['persons']),20)

    def test_unsourced_membership_refused(self):
        Affiliation.objects.filter(person=self.people[19]).update(source_document=None)
        with self.assertRaisesMessage(ValidationError,'unsourced_membership'):self.selected()

    def test_multiple_acts_rejected(self):
        other=self.make_scrutiny(self.make_document())
        with self.assertRaisesMessage(ValidationError,'requires_one_act'):
            build_catalogue([self.scrutiny.stable_id,other.stable_id],collective_group=self.group)

    def test_conflicting_votes_both_preserved(self):
        VoteObservation.objects.create(person=self.people[0],scrutiny=self.scrutiny,source_document=self.make_document(),
            source_category='against',normalized_category='against',mapping_rule='SYNTH')
        self.assertEqual(len(self.selected()['vote_observations']),18)

    def test_extension_requires_explicit_version(self):
        p=self.payload('speech_vote');p['catalogue']=self.selected()
        with self.assertRaises(ValidationError):validate_speech_vote_payload(p)
        p['public_contract_version']='dv.content/0.4.0-dvverite-draft.1'
        validate_speech_vote_payload(p)
        p['catalogue']['affiliations'][0]['observed_on']=None
        validate_speech_vote_payload(p)
        p['public_contract_version']='unknown'
        with self.assertRaisesMessage(ValidationError,'public_contract_unsupported'):validate_speech_vote_payload(p)

    def make_bound(self):
        passage=EvidencePassage.objects.create(document=self.doc,exact_text='SYNTH.',locator='p1',public_url=self.doc.source.document_url)
        declaration=Declaration.objects.create(stable_id='DECL:SYNTH',speaker_person=self.people[0],speaker_organization=self.group,
            speaker_label='SYNTH',capacity='SYNTH',exact_quote=passage.exact_text,occurred_on=self.scrutiny.occurred_on,
            source_passage=passage,position_scope='collective')
        comparison=Comparison.objects.create(stable_id='SYNTH:C',declaration=declaration,result='inconclusive',
            criteria={'catalogue_scope':{'contract':'dv.catalogue-selection/1','kind':'dated_collective_members'}})
        ComparisonAct.objects.create(comparison=comparison,scrutiny=self.scrutiny,chronological_relation='same_day')
        p,refs=prepare_public_snapshot('speech_vote',{},self.snapshot(self.doc),comparison=comparison)
        d=EditorialDossier.objects.create(stable_id='SYNTH:SCOPED',content_kind='speech_vote',established_kind='speech_vote',data_scope='real')
        r=EditorialRevision.objects.create(dossier=d,revision=1,comparison=comparison,public_payload=p,source_snapshot=refs)
        self.assertTrue(binding_check(r)[0])
        return r

    def test_client_omission_or_foreign_actor_rejected(self):
        r=self.make_bound();original=deepcopy(r.public_payload)
        for ids in [original['catalogue']['selection']['scope_person_ids'][:-1],['SYNTH:FOREIGN']]:
            r.public_payload=deepcopy(original)
            r.public_payload['catalogue']['selection']['scope_person_ids']=ids
            self.assertFalse(binding_check(r)[0])

    def test_changed_membership_invalidates_bound_projection(self):
        r=self.make_bound()
        Affiliation.objects.filter(person=self.people[19]).update(ends_on=self.scrutiny.occurred_on-timedelta(days=1),starts_on=self.scrutiny.occurred_on-timedelta(days=2))
        self.assertFalse(binding_check(r)[0])

    def test_client_scope_list_in_criteria_rejected(self):
        r=self.make_bound();r.comparison.criteria['catalogue_scope']['scope_person_ids']=['SYNTH:1']
        r.comparison.save(update_fields=['criteria'])
        self.assertFalse(binding_check(r)[0])
