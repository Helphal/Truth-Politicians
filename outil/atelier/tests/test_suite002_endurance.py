"""Bounded synthetic import endurance; no repeated real AMO import."""
import hashlib,json,time,zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.models import Person,ArchiveScan
from atelier.services.reference_resolution import import_reference_archive,resolve_record

class BoundedImportTests(AtelierTestCase):
    def test_75_records_bounded_batches_rollback_restart_idempotence(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.zip'
            with zipfile.ZipFile(path,'w') as z:
                for i in range(75):
                    z.writestr(f'acteur/{i:03}.json',json.dumps({'acteur':{'uid':{'#text':f'PA{i+1:06}'},'etatCivil':{'ident':{'prenom':'Fictif','nom':f'Individu {i}'}}}}))
            doc=self.make_document();doc.sha256=hashlib.sha256(path.read_bytes()).hexdigest();doc.save()
            first=import_reference_archive(doc,path,batch_size=7);self.assertEqual(first['cursor'],7)
            def interrupted(document,kind,item,locator):
                if item['ref']=='PA000010':raise ValidationError('bounded_synthetic_interruption')
                return resolve_record(document,kind,item,locator)
            with patch('atelier.services.reference_resolution.resolve_record',side_effect=interrupted):
                with self.assertRaises(ValidationError):import_reference_archive(doc,path,batch_size=7)
            self.assertEqual(Person.objects.count(),7);self.assertEqual(ArchiveScan.objects.get().cursor,7)
            cursors=[7]
            for _ in range(10):
                result=import_reference_archive(doc,path,batch_size=7);cursors.append(result['cursor'])
                if result['complete']:break
            self.assertEqual(cursors,[7,14,21,28,35,42,49,56,63,70,75])
            for _ in range(3):self.assertEqual(import_reference_archive(doc,path,batch_size=7)['decisions'],{})
            self.assertEqual(Person.objects.count(),75)
