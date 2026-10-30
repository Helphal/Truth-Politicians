from datetime import date
from tempfile import TemporaryDirectory
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_night_staging
from atelier.models import Scrutiny,StagedScrutiny,ScrutinyIdentityCorrection,HumanReview
from atelier.services.archive_staging import stage_archive
from atelier.services.identity_corrections import separate_congress_collision
from atelier.services.materialization import materialize_staged
from atelier.services.catalogue import official_fields

class IdentityCorrectionTests(AtelierTestCase):
    archive=test_night_staging.NightStagingTests.archive
    record=test_night_staging.NightStagingTests.record
    def fixture(self,tmp):
        an=self.record();an['dateScrutin']='2022-07-11'
        cg=self.record('VTCGR5L16V1');cg['dateScrutin']='2024-03-04'
        path,doc=self.archive(tmp,[an,cg]);stage_archive(doc,path)
        old=self.make_scrutiny(doc,source_identifier='1');old.stable_id='AN:16:SCRUTIN:1';old.occurred_on=date(2024,3,4);old.save()
        evidence={'assembly_vote_url':'https://www.assemblee-nationale.fr/dyn/16/scrutins/1','congress_vote_url':'https://www.assemblee-nationale.fr/dyn/16/scrutins/c1','discovered_from':'official legislative dossier (synthetic test)'}
        return old,StagedScrutiny.objects.get(source_uid=an['uid']),StagedScrutiny.objects.get(source_uid=cg['uid']),dict(archive_path=path,evidence=evidence)
    def test_distinct_namespace_preserves_legacy_and_replays(self):
        with TemporaryDirectory() as tmp:
            old,an,cg,kwargs=self.fixture(tmp)
            result,created=separate_congress_collision(old,an,cg,**kwargs)
            again,replayed=separate_congress_collision(old,an,cg,**kwargs)
            self.assertTrue(created);self.assertFalse(replayed);self.assertEqual(result.pk,again.pk)
            self.assertEqual(Scrutiny.objects.count(),3);self.assertEqual(result.original_snapshot['institution'],'Assemblée nationale')
            old.refresh_from_db();self.assertEqual(old.stable_id,'AN:16:SCRUTIN:1')
            with self.assertRaises(ValidationError):official_fields(old)
            with self.assertRaises(ValidationError):result.save()
            for staged in [an,cg]:
                staged.refresh_from_db();fact,added=materialize_staged(staged,kwargs['archive_path'])
                self.assertFalse(added);self.assertEqual(fact.institution,staged.institution)
                self.assertEqual(fact.observations.count(),0)
            self.assertEqual(HumanReview.objects.count(),0)
    def test_wrong_route_rejected_without_partial_changes(self):
        with TemporaryDirectory() as tmp:
            old,an,cg,kwargs=self.fixture(tmp);kwargs['evidence']['congress_vote_url']=kwargs['evidence']['assembly_vote_url']
            with self.assertRaises(ValidationError):separate_congress_collision(old,an,cg,**kwargs)
            old.refresh_from_db();self.assertEqual(old.institution,'Assemblée nationale')
            self.assertEqual(Scrutiny.objects.count(),1);self.assertFalse(ScrutinyIdentityCorrection.objects.exists())
    def test_modified_pinned_member_rejected_before_repair(self):
        with TemporaryDirectory() as tmp:
            old,an,cg,kwargs=self.fixture(tmp);cg.raw_sha256='0'*64
            with self.assertRaisesMessage(ValidationError,'correction_member_digest_mismatch'):separate_congress_collision(old,an,cg,**kwargs)
            self.assertFalse(ScrutinyIdentityCorrection.objects.exists())
