import hashlib, json, zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.exceptions import ValidationError
from atelier.models import ArchiveScan, ReferenceResolution, Person
from atelier.services.reference_resolution import import_reference_archive
from atelier.tests.support import AtelierTestCase

class ReferenceBatchTests(AtelierTestCase):
    def archive(self,folder,duplicate=False):
        path=Path(folder)/'fixture.zip'
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for i in range(3):
                z.writestr(f'acteur/{i}.json',json.dumps({'acteur':{'uid':{'#text':f'PA{1 if duplicate else i+1}'},'etatCivil':{'ident':{'prenom':'Personne','nom':f'fictive {i}'},'dateNaissance':'PRIVATE_CANARY'},'adresses':'PRIVATE_CANARY'}}))
        doc=self.make_document();doc.sha256=hashlib.sha256(path.read_bytes()).hexdigest();doc.save()
        return doc,path
    def test_interrupt_resume_three_replays_no_duplicate(self):
        with TemporaryDirectory() as folder:
            doc,path=self.archive(folder)
            result=import_reference_archive(doc,path,stop_requested=lambda:True)
            self.assertEqual(result['cursor'],0)
            result=import_reference_archive(doc,path,batch_size=1)
            self.assertEqual(result['cursor'],1)
            result=import_reference_archive(doc,path)
            self.assertTrue(result['complete'])
            for _ in range(3):self.assertEqual(import_reference_archive(doc,path)['decisions'],{})
            self.assertEqual(Person.objects.count(),3)
            self.assertNotIn('PRIVATE_CANARY',json.dumps(list(ReferenceResolution.objects.values('proposed'))))
    def test_failure_rolls_back_cursor_and_decisions(self):
        with TemporaryDirectory() as folder:
            doc,path=self.archive(folder)
            from atelier.services.reference_resolution import resolve_record
            def fail(document,kind,item,locator):
                if item['ref']=='PA2':raise ValidationError('synthetic_interruption')
                return resolve_record(document,kind,item,locator)
            with patch('atelier.services.reference_resolution.resolve_record',side_effect=fail):
                with self.assertRaises(ValidationError):import_reference_archive(doc,path)
            self.assertEqual(Person.objects.count(),0);self.assertEqual(ArchiveScan.objects.count(),0)
            self.assertTrue(import_reference_archive(doc,path)['complete'])
    def test_duplicate_source_id_rejected(self):
        with TemporaryDirectory() as folder:
            doc,path=self.archive(folder,duplicate=True)
            with self.assertRaisesMessage(ValidationError,'reference_duplicate_identifier'):import_reference_archive(doc,path)
            self.assertEqual(Person.objects.count(),0)
    def test_source_tamper_rejected(self):
        with TemporaryDirectory() as folder:
            doc,path=self.archive(folder);path.write_bytes(path.read_bytes()+b'changed')
            with self.assertRaisesMessage(ValidationError,'archive_digest_mismatch'):import_reference_archive(doc,path)
    def test_bounds_not_disableable(self):
        with TemporaryDirectory() as folder:
            doc,path=self.archive(folder)
            for kwargs in ({'batch_size':1001},{'max_records':50001},{'max_seconds':61}):
                with self.assertRaises(ValidationError):import_reference_archive(doc,path,**kwargs)
