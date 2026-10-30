import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from django.test import SimpleTestCase
from scripts.evidence_bytes import write_evidence, verify_logs, verify_archive_logs


class RawEvidenceTests(SimpleTestCase):
    def test_lf_crlf_disk_and_archive(self):
        for ending in ('\n', '\r\n'):
            with self.subTest(ending=repr(ending)), TemporaryDirectory() as temp:
                root = Path(temp)
                digest = write_evidence(root/'test.txt', 'one'+ending+'two'+ending)
                self.assertEqual(digest, hashlib.sha256((root/'test.txt').read_bytes()).hexdigest())
                write_evidence(root/'resultats.json', json.dumps({'steps': [{'step': 'test', 'log_sha256': digest}]}))
                self.assertTrue(verify_logs(root)['passed'])
                with zipfile.ZipFile(root/'proof.zip', 'w') as archive:
                    archive.write(root/'test.txt', 'test.txt')
                    archive.write(root/'resultats.json', 'resultats.json')
                self.assertTrue(verify_archive_logs(root/'proof.zip')['passed'])

    def test_one_byte_changed_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            digest = write_evidence(root/'test.txt', 'original\n')
            write_evidence(root/'resultats.json', json.dumps({'steps': [{'step': 'test', 'log_sha256': digest}]}))
            (root/'test.txt').write_bytes(b'Original\n')
            self.assertFalse(verify_logs(root)['passed'])

    def test_old_normalized_hash_not_accepted(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_evidence(root/'test.txt', 'old\r\n')
            normalized = hashlib.sha256(b'old\n').hexdigest()
            write_evidence(root/'resultats.json', json.dumps({'steps': [{'step': 'test', 'log_sha256': normalized}]}))
            self.assertFalse(verify_logs(root)['passed'])

    def test_evidence_cannot_be_overwritten(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/'test.txt'
            write_evidence(path, 'first')
            with self.assertRaises(FileExistsError):
                write_evidence(path, 'replacement')
