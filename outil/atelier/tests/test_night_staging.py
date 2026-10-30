import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.models import ArchiveScan, StagedScrutiny
from atelier.services.archive_staging import stage_archive

class NightStagingTests(AtelierTestCase):
    def archive(self,root,records):
        path=Path(root)/'synthetic.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for i,r in enumerate(records):
                archive.writestr(f'{i:04}.json',json.dumps({'scrutin':r}))
        doc=self.make_document(sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return path,doc
    def record(self,uid='VTANR5L16V1',number='1'):
        return {'uid':uid,'numero':number,'legislature':'16','dateScrutin':'2024-01-01','titre':'FICTIF TEST',
            'syntheseVote':{'nombreVotants':'3','suffragesExprimes':'2','decompte':{'pour':'1','contre':'1','abstentions':'1'}}}
    def test_bounded_resume_no_fake_text_or_duplicate(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record(),self.record('VTANR5L16V2','2')])
            first=stage_archive(doc,path,batch_size=1)
            second=stage_archive(doc,path,batch_size=1)
            third=stage_archive(doc,path)
        self.assertEqual((first['cursor_after'],second['cursor_after'],third['added']),(1,2,0))
        self.assertTrue(third['completed'])
        self.assertEqual(StagedScrutiny.objects.count(),2)
        self.assertTrue(all(r.normalized['public_eligible'] is False for r in StagedScrutiny.objects.all()))
    def test_congress_collision_separate_and_preserves_legacy(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record(),self.record('VTCGR5L16V1')])
            old=self.make_scrutiny(doc,source_identifier='1')
            old.official_title='FICTIF TEST';old.save()
            stage_archive(doc,path)
        congress=StagedScrutiny.objects.get(source_uid='VTCGR5L16V1')
        self.assertEqual(congress.legacy_scrutiny,old)
        self.assertIsNone(congress.normalized['official_vote_url'])
        old.refresh_from_db()
        self.assertEqual(old.institution,'Assemblée nationale')
        self.assertEqual(StagedScrutiny.objects.count(),2)
    def test_wrong_digest_refused_before_writing(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record()]);doc.sha256='0'*64
            with self.assertRaisesMessage(ValidationError,'archive_digest_mismatch'):stage_archive(doc,path)
        self.assertEqual(ArchiveScan.objects.count(),0)
    def test_cooperative_stop_no_cursor_advance(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record()])
            result=stage_archive(doc,path,stop_requested=lambda:True)
        self.assertTrue(result['cooperative_stop']);self.assertEqual(result['cursor_after'],0)
    def test_invalid_uid_rolls_back_entire_batch(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record(),self.record('INVALID','2')])
            with self.assertRaisesMessage(ValidationError,'scrutiny_uid_namespace_invalid'):stage_archive(doc,path)
        self.assertEqual(StagedScrutiny.objects.count(),0);self.assertEqual(ArchiveScan.objects.count(),0)
    def test_source_quarantine_refuses_resume(self):
        with TemporaryDirectory() as tmp:
            path,doc=self.archive(tmp,[self.record()]);doc.quarantined=True
            with self.assertRaisesMessage(ValidationError,'archive_source_unavailable'):stage_archive(doc,path)
