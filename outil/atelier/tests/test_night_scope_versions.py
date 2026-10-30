from django.test import override_settings
from django.core.exceptions import ValidationError
from atelier.models import CatalogueScopeVersion,CatalogueTransfer,Person
from atelier.services.catalogue_transfer import build_catalogue_transfer,receive_catalogue_page
from atelier.tests.support import AtelierTestCase
import review_followup as q

@override_settings(DV_ENV='test',DV_EVENT_KEY_ID='test-v1',DV_REVIEW_ATTESTATION_KEY='synthetic-pagination-only')
class ScopeVersionTests(AtelierTestCase):
    make_catalogue_revision=q.FollowupTests.make_catalogue_revision
    def complete(self,manifest,pages):
        for page in reversed(pages):result=receive_catalogue_page(manifest,page)
        return result
    def test_missing_page_never_activates_then_atomic_complete(self):
        _,_,_,scrutiny=self.make_catalogue_revision()
        manifest,pages=build_catalogue_transfer([scrutiny.stable_id],page_size=1,generation=1)
        receive_catalogue_page(manifest,pages[-1])
        self.assertFalse(CatalogueScopeVersion.objects.exists())
        result=self.complete(manifest,pages)
        self.assertEqual(result['local_scope_activation'],'activated_locally_immutable_scope')
        self.assertFalse(result['site_activated'])
    def test_late_old_snapshot_cannot_overwrite_new_person(self):
        _,_,_,scrutiny=self.make_catalogue_revision()
        old,oldpages=build_catalogue_transfer([scrutiny.stable_id],generation=1)
        person=Person.objects.first();old_name=person.display_name;person.display_name='Nouveau libellé fictif';person.save()
        new,newpages=build_catalogue_transfer([scrutiny.stable_id],generation=2)
        self.complete(new,newpages);result=self.complete(old,oldpages)
        self.assertEqual(result['local_scope_activation'],'stale_generation')
        state=CatalogueScopeVersion.objects.select_related('transfer').get()
        names=[p['display_name'] for p in state.transfer.assembled_catalogue['persons']]
        self.assertIn('Nouveau libellé fictif',names);self.assertNotIn(old_name,names)
        self.assertEqual(CatalogueTransfer.objects.count(),2)
    def test_late_page_cannot_unsuspend(self):
        _,_,_,scrutiny=self.make_catalogue_revision()
        m,p=build_catalogue_transfer([scrutiny.stable_id],generation=1)
        self.complete(m,p);state=CatalogueScopeVersion.objects.get();state.suspended=True;state.save()
        m2,p2=build_catalogue_transfer([scrutiny.stable_id],generation=2)
        self.assertEqual(self.complete(m2,p2)['local_scope_activation'],'suspension_latched')
        state.refresh_from_db();self.assertTrue(state.suspended);self.assertEqual(state.generation,1)
    def test_same_generation_different_snapshot_conflicts(self):
        _,_,_,scrutiny=self.make_catalogue_revision();m,p=build_catalogue_transfer([scrutiny.stable_id],generation=1);self.complete(m,p)
        person=Person.objects.first();person.display_name='Conflit fictif';person.save()
        other,pages=build_catalogue_transfer([scrutiny.stable_id],generation=1)
        self.assertEqual(self.complete(other,pages)['local_scope_activation'],'generation_conflict')
    def test_version_mix_and_unsigned_generation_rejected(self):
        _,_,_,scrutiny=self.make_catalogue_revision();m,p=build_catalogue_transfer([scrutiny.stable_id],generation=1)
        p[0]['format']='dv.catalogue-transfer/0.1.0-draft.1'
        with self.assertRaisesMessage(ValidationError,'catalogue_page_wrong_snapshot'):receive_catalogue_page(m,p[0])
        m['generation']=2
        with self.assertRaisesMessage(ValidationError,'catalogue_manifest_unauthorized'):receive_catalogue_page(m,p[0])
