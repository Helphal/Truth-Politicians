from tempfile import TemporaryDirectory
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_night_staging
from atelier.models import StagedScrutiny,FactQualification,Scrutiny
from atelier.services.archive_staging import stage_archive
from atelier.services.materialization import materialize_staged,qualify
from atelier.services.catalogue import official_fields

class MaterializationTests(AtelierTestCase):
    archive=test_night_staging.NightStagingTests.archive
    record=test_night_staging.NightStagingTests.record
    def test_three_replays_block_exact_act_and_immutable_qualification(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record()]);stage_archive(doc,path)
            staged=StagedScrutiny.objects.get();results=[materialize_staged(staged,path) for _ in range(3)]
        self.assertEqual([x[1] for x in results],[True,False,False]);self.assertEqual(Scrutiny.objects.count(),1)
        q=FactQualification.objects.get();self.assertEqual(q.state,'blocked')
        with self.assertRaises(ValidationError):q.save()
        with self.assertRaises(ValidationError):official_fields(results[0][0])
    def test_congress_refused_without_guessed_url(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record('VTCGR5L16V1')]);stage_archive(doc,path)
            with self.assertRaisesMessage(ValidationError,'congress_exact_vote_route_required'):materialize_staged(StagedScrutiny.objects.get(),path)
        self.assertFalse(Scrutiny.objects.exists())
    def test_member_digest_and_document_quarantine_fail_before_write(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record()]);stage_archive(doc,path);s=StagedScrutiny.objects.get()
            s.raw_sha256='0'*64
            with self.assertRaisesMessage(ValidationError,'materialization_member_digest_mismatch'):materialize_staged(s,path)
            s.document.quarantined=True
            with self.assertRaisesMessage(ValidationError,'materialization_source_unavailable'):materialize_staged(s,path)
        self.assertFalse(Scrutiny.objects.exists())
    def test_qualification_cannot_be_passed_with_missing_pieces(self):
        s=self.make_scrutiny()
        with self.assertRaises(ValidationError):qualify(s,state='qualified',object_type='act',expected_pieces=['missing'],references=['official'],explanation='')
