from copy import deepcopy
from datetime import timedelta
from django.core.exceptions import ValidationError
from atelier.tests import test_catalogue_collective_scope as fixtures
from atelier.models import Affiliation, EditorialRevision
from atelier.services.catalogue import binding_check
from atelier.services.contracts import validate_speech_vote_payload
from atelier.services.full_review import build_review_bundle, DVVERITE_FORMAT

class IndependentFinalCountertests(fixtures.CollectiveScopeTests):
    def test_coordinated_client_removal_of_nonvoter_still_refused(self):
        r=self.make_bound();p=r.public_payload;sid=self.people[19].stable_id
        p['catalogue']['persons']=[x for x in p['catalogue']['persons'] if x['id']!=sid]
        p['catalogue']['affiliations']=[x for x in p['catalogue']['affiliations'] if x['person_id']!=sid]
        p['catalogue']['selection']['scope_person_ids'].remove(sid)
        self.assertFalse(binding_check(r)[0])
        self.assertEqual(Affiliation.objects.filter(person=self.people[19]).count(),1)

    def test_forged_group_or_day_cannot_relabel_identical_people(self):
        r=self.make_bound();original=deepcopy(r.public_payload)
        for key,value in [('group_id','SYNTH:FALSEGROUP'),('as_of',(self.scrutiny.occurred_on+timedelta(days=1)).isoformat())]:
            r.public_payload=deepcopy(original);r.public_payload['catalogue']['selection'][key]=value
            self.assertFalse(binding_check(r)[0])

    def test_new_missing_vote_member_invalidates_existing_bound_scope(self):
        r=self.make_bound()
        from atelier.models import Person
        person=Person.objects.create(stable_id='SYNTH:NEW-NONVOTER',display_name='SYNTH new nonvoter')
        Affiliation.objects.create(person=person,organization=self.group,status='SYNTH',starts_on=self.scrutiny.occurred_on,observed_on=None,source_document=self.doc)
        self.assertFalse(binding_check(r)[0])
        new=self.selected();self.assertEqual(len(new['selection']['scope_person_ids']),21)
        self.assertEqual(len(new['vote_observations']),17)

    def test_null_observation_is_unknown_and_profile_never_invents_date(self):
        Affiliation.objects.update(observed_on=None)
        p=self.payload('speech_vote');p['catalogue']=self.selected();p['public_contract_version']='dv.content/0.4.0-dvverite-draft.1'
        self.assertTrue(all(x['observed_on'] is None for x in p['catalogue']['affiliations']))
        validate_speech_vote_payload(p)
        old=deepcopy(p);del old['public_contract_version'];del old['catalogue']['selection']
        with self.assertRaises(ValidationError):validate_speech_vote_payload(old)
        bad=deepcopy(p);bad['catalogue']['affiliations'][0]['observed_on']='2024-99-99'
        with self.assertRaises(ValidationError):validate_speech_vote_payload(bad)

    def test_changed_observation_fact_invalidates_binding_without_omission(self):
        r=self.make_bound();r.public_payload['catalogue']['affiliations'][0]['observed_on']=None
        self.assertFalse(binding_check(r)[0])

    def test_full_review_extended_payload_is_exact_and_unsaved_mutation_ignored(self):
        p=self.payload('speech_vote');p['public_contract_version']='dv.content/0.4.0-dvverite-draft.1'
        _,r,_=self.make_revision('speech_vote',payload=p)
        bundle=build_review_bundle(r)
        self.assertEqual(bundle['format'],DVVERITE_FORMAT);self.assertEqual(bundle['public'],p)
        r.public_payload['main_caveat']='SYNTH changed after digest'
        self.assertEqual(build_review_bundle(r),bundle)

    def test_full_review_persisted_payload_corruption_is_refused(self):
        p=self.payload('speech_vote');p['public_contract_version']='dv.content/0.4.0-dvverite-draft.1'
        _,r,_=self.make_revision('speech_vote',payload=p)
        build_review_bundle(r)
        altered=deepcopy(r.public_payload)
        altered['main_caveat']='SYNTH changed after digest'
        # Bypass normal revision creation to simulate corrupt persisted storage.
        EditorialRevision.objects.filter(pk=r.pk).update(public_payload=altered)
        with self.assertRaisesRegex(ValidationError,'full_review_bundle_invalid'):
            build_review_bundle(r)
