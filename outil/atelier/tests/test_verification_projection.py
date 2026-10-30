from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from atelier.services.verification import structural_metadata
from atelier.services.catalogue import official_fields, apply_speech_vote_qualifications
from atelier.models import Person
from .support import AtelierTestCase


class ProjectionMetadataTests(SimpleTestCase):
    def test_nested_reference_requires_complete_received_tuple(self):
        reference={'source_id':'SYNTH','version_id':'v1','sha256':'a'*64,'public_url':'https://example.org/source'}
        value={'content':{'source_snapshot':[reference], 'public_payload':{'catalogue':{'scrutinies':[{'source_version':deepcopy(reference)}]}}},'sources':[]}
        claim={'location':'/public_payload/catalogue/scrutinies/0/source_version/source_id','value':'SYNTH'}
        self.assertTrue(structural_metadata(claim,value))
        value['content']['public_payload']['catalogue']['scrutinies'][0]['source_version']['sha256']='b'*64
        self.assertFalse(structural_metadata(claim,value))

    def test_pagination_is_recomputed_and_does_not_cover_vote_counts(self):
        page={'limit':250,'offset':0,'returned_count':1,'total_count_in_materialized_scope':1,'complete_for_materialized_scope':True}
        value={'content':{'public_payload':{'catalogue':{'pagination':page,'vote_observations':[{}]}}},'sources':[]}
        claim={'location':'/public_payload/catalogue/pagination/returned_count','value':1}
        self.assertTrue(structural_metadata(claim,value))
        page['total_count_in_materialized_scope']=2
        self.assertFalse(structural_metadata(claim,value))
        claim['location']='/public_payload/catalogue/scrutinies/0/counts/for/value'
        self.assertFalse(structural_metadata(claim,value))

    def test_actor_identity_is_not_a_structural_exemption(self):
        value={'content':{},'sources':[]}
        for path in ['/public_payload/catalogue/persons/0/id','/public_payload/catalogue/vote_observations/0/person_id','/public_payload/attribution/person_id']:
            self.assertFalse(structural_metadata({'location':path,'value':'SYNTH-OTHER'},value))


class SenateSessionProjectionTests(AtelierTestCase):
    def test_senate_internal_namespace_is_not_exposed_as_legislature(self):
        document=self.make_document()
        scrutiny=self.make_scrutiny(document)
        scrutiny.institution='Sénat';scrutiny.legislature='2025-2026';scrutiny.session='2025-2026';scrutiny.save()
        value=official_fields(scrutiny)
        self.assertEqual(value['legislature'],'Non renseignée dans cette fiche ; voir la session')
        self.assertEqual(value['session'],'2025-2026')
        scrutiny.refresh_from_db()
        self.assertEqual(scrutiny.legislature,'2025-2026')

    def project_collective(self, person_pk):
        material={'qualified_declaration':{'state':'known','precision':'day','value':'2026-01-29'},
                  'actors':{'scope':'collective'},'collective_vote_distribution':{
                  'state':'group_distribution','voices':[{'person_id':person_pk,'category':'against'}],
                  'exclusions':[{'person_id':person_pk,'reason':'SYNTH exclusion'}]}}
        comparison=MagicMock()
        comparison.declaration=SimpleNamespace(exact_quote='SYNTH',source_passage=SimpleNamespace(public_url='https://example.org'))
        comparison.acts.select_related.return_value.order_by.return_value.first.return_value=None
        with patch('atelier.services.thematic_integrity.qualification_material_for_comparison',return_value=material):
            result=apply_speech_vote_qualifications({},comparison)
        return material,result

    def test_collective_public_voices_join_catalogue_by_stable_identity(self):
        person=Person.objects.create(stable_id='SYNTH:PERSON:A',display_name='SYNTH A')
        original,result=self.project_collective(person.pk)
        for key in ['voices','exclusions']:
            self.assertEqual(result['qualification_material']['collective_vote_distribution'][key][0]['person_id'],person.stable_id)
            self.assertEqual(original['collective_vote_distribution'][key][0]['person_id'],person.pk)
        self.assertEqual(result['collective_distribution']['included_member_ids'],[person.stable_id])

    def test_missing_identity_refuses_projection(self):
        with self.assertRaisesMessage(ValidationError,'collective_person_projection_missing'):
            self.project_collective(999999)
