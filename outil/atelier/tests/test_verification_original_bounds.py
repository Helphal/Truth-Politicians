import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from django.test import SimpleTestCase, override_settings
from atelier.services.verification import source_integrity, MAX_ORIGINAL_BYTES


class OriginalBoundsTests(SimpleTestCase):
    def test_original_larger_than_eight_mib_is_hashed_and_still_bounded(self):
        with TemporaryDirectory() as folder, override_settings(DV_EVIDENCE_VAULT_PATH=Path(folder)):
            root=Path(folder); original=root/'official.zip'; text=root/'selected.txt'
            text.write_text('SYNTH institutional fields only.',encoding='utf-8')
            with original.open('wb') as stream:stream.truncate(MAX_ORIGINAL_BYTES)
            with original.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            document=SimpleNamespace(source=SimpleNamespace(family='institutional_document'),original_path=str(original),
                sha256=digest,collection_receipt={'normalized_text_file':text.name,'normalized_text_sha256':hashlib.sha256(text.read_bytes()).hexdigest()})
            passages=[{'exact_text':'SYNTH institutional fields only.'}]
            self.assertEqual(source_integrity(document,passages),'original_and_passages_checked')
            document.sha256='0'*64
            self.assertEqual(source_integrity(document,passages),'original_hash_mismatch')
            with original.open('ab') as stream:stream.write(b'x')
            self.assertEqual(source_integrity(document,passages),'original_missing_or_outside_vault')

    def test_text_does_not_inherit_larger_original_limit(self):
        with TemporaryDirectory() as folder, override_settings(DV_EVIDENCE_VAULT_PATH=Path(folder)):
            root=Path(folder); original=root/'original'; text=root/'selected.txt'
            original.write_bytes(b'SYNTH')
            with text.open('wb') as stream:stream.truncate(8*1024*1024+1)
            doc=SimpleNamespace(source=SimpleNamespace(family='institutional_document'),original_path=str(original),
                sha256=hashlib.sha256(b'SYNTH').hexdigest(),collection_receipt={'normalized_text_file':text.name})
            self.assertEqual(source_integrity(doc,[{'exact_text':'SYNTH'}]),'normalized_text_missing')
