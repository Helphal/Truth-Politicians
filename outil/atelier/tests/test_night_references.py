from django.core.exceptions import ValidationError
from atelier.models import Person,Mandate,Affiliation,ReferenceResolution,Organization
from atelier.services.reference_resolution import resolve_record,minimal_record,_records_from_member
from atelier.tests.support import AtelierTestCase

class NightReferenceTests(AtelierTestCase):
    def actor(self,doc,ref='PA1',name='Personne fictive'):
        return resolve_record(doc,'acteur',{'ref':ref,'name':name},'synthetic.json')
    def mandate(self,ref='PM1',person='PA1',start='2022-06-22',end='2024-06-09',kind='ASSEMBLEE',groups=None):
        return {'ref':ref,'person_ref':person,'type':kind,'start':start,'end':end,'end_field_present':True,'organ_refs':groups or []}
    def test_unknown_to_resolved_keeps_previous_and_idempotent(self):
        doc=self.make_document()
        Person.objects.create(stable_id='AN:PERSON:PA1',display_name='non résolu',identity_status='unresolved',institutional_ids={'assemblee_nationale':'PA1'})
        self.assertEqual(self.actor(doc),('resolved',True));self.assertEqual(self.actor(doc),('resolved',False))
        self.assertEqual(ReferenceResolution.objects.get().previous['display_name'],'non résolu')
    def test_homonyms_not_merged(self):
        doc=self.make_document();self.actor(doc);self.actor(doc,'PA2')
        self.assertEqual(Person.objects.count(),2)
    def test_conflicting_name_preserved(self):
        doc=self.make_document();self.actor(doc);other=self.make_document()
        self.assertEqual(self.actor(other,name='Autre identité')[0],'conflict')
        self.assertEqual(Person.objects.get().display_name,'Personne fictive')
        third=self.make_document()
        self.assertEqual(self.actor(third,name='Troisième proposition')[0],'conflict')
        self.assertEqual(Person.objects.get().display_name,'Personne fictive')
    def test_multiple_mandates_and_date_bounds(self):
        doc=self.make_document();self.actor(doc)
        resolve_record(doc,'mandat',self.mandate(),'m1')
        resolve_record(doc,'mandat',self.mandate('PM2',start='2024-07-08',end=None),'m2')
        self.assertEqual(Mandate.objects.count(),2)
    def test_missing_actor_no_invention(self):
        doc=self.make_document();status,_=resolve_record(doc,'mandat',self.mandate(),'m')
        self.assertEqual(status,'pending');self.assertEqual(Mandate.objects.count(),0)
    def test_contradictory_dates_no_mandate(self):
        doc=self.make_document();self.actor(doc)
        status,_=resolve_record(doc,'mandat',self.mandate(start='2025-01-01',end='2024-01-01'),'m')
        self.assertEqual(status,'conflict');self.assertEqual(Mandate.objects.count(),0)
    def test_unknown_end_not_assumed_open(self):
        doc=self.make_document();self.actor(doc);item=self.mandate(end=None);item['end_field_present']=False
        self.assertEqual(resolve_record(doc,'mandat',item,'m')[0],'pending')
        self.assertEqual(Mandate.objects.count(),0)
    def test_group_change_separate_intervals_no_party(self):
        doc=self.make_document();self.actor(doc)
        for i in (1,2):
            resolve_record(doc,'organe',{'ref':f'PO{i}','type':'GP','name':f'Groupe fictif {i}','start':None,'end':None},'org')
            resolve_record(doc,'mandat',self.mandate(f'PM{i}',start=f'202{i}-01-01',end=f'202{i}-12-31',kind='GP',groups=[f'PO{i}']),'m')
        self.assertEqual(Affiliation.objects.count(),2);self.assertEqual(Organization.objects.filter(kind='party').count(),0)
    def test_amo_acteur_file_yields_nested_mandats(self):
        payload={
            'acteur':{
                'uid':'PA1',
                'etatCivil':{'ident':{'prenom':'A','nom':'B'}},
                'mandats':{'mandat':[
                    {'uid':'PM1','acteurRef':'PA1','typeOrgane':'ASSEMBLEE','dateDebut':'2024-07-08','dateFin':None},
                    {'uid':'PM2','acteurRef':'PA1','typeOrgane':'GP','dateDebut':'2024-07-19','dateFin':None,'organes':{'organeRef':'PO1'}},
                ]},
            }
        }
        kinds=[k for k,_,_ in _records_from_member('json/acteur/PA1.json',payload)]
        self.assertEqual(kinds,['acteur','mandat','mandat'])

    def test_personal_data_minimized(self):
        row={'uid':{'#text':'PA1'},'etatCivil':{'ident':{'prenom':'Prénom','nom':'Nom'},'dateNaissance':'SECRET_CANARY'},'adresses':'PRIVATE_CANARY'}
        self.assertEqual(minimal_record('acteur',row),{'ref':'PA1','name':'Prénom Nom'})
    def test_decision_immutable(self):
        doc=self.make_document();self.actor(doc);decision=ReferenceResolution.objects.get()
        with self.assertRaisesMessage(ValidationError,'reference_resolution_immutable'):decision.save()
