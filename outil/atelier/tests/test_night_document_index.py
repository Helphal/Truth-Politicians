import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.exceptions import ValidationError
from atelier.models import IndexedPassage
from atelier.services.document_index import index_html,search_passages
from atelier.tests.support import AtelierTestCase

class NightDocumentIndexTests(AtelierTestCase):
    def index(self,contents):
        data=contents.encode();doc=self.make_document(sha256=hashlib.sha256(data).hexdigest())
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.html';path.write_bytes(data);result=index_html(doc,path)
        return doc,result
    def test_script_and_navigation_not_indexed(self):
        _,result=self.index('<script><p>SECRET_CANARY '+('x'*40)+'</p></script><nav><p>'+('NAV_CANARY '*8)+'</p></nav><p>Cette phrase fictive porte sur le prix de l’électricité.</p>')
        self.assertEqual(result['passages'],1)
        self.assertNotIn('CANARY',IndexedPassage.objects.get().visible_text)
        self.assertEqual(len(search_passages(query='prix electricite')['results']),1)
    def test_stable_cursor_continues_without_duplicate(self):
        self.index(''.join(f'<p>Le prix de l’électricité fait débat, exemple fictif numéro {i}.</p>' for i in range(3)))
        first=search_passages(query='prix',limit=1);second=search_passages(query='prix',limit=1,cursor=first['next_cursor'])
        self.assertNotEqual(first['results'][0]['id'],second['results'][0]['id'])
        self.assertEqual(first['corpus_version'],second['corpus_version'])
    def test_corpus_insertion_invalidates_cursor(self):
        self.index('<p>'+('prix exemple '*10)+'</p><p>'+('prix autre '*10)+'</p>')
        first=search_passages(query='prix',limit=1)
        self.index('<p>'+('nouveau prix '*10)+'</p>')
        with self.assertRaisesMessage(ValidationError,'passage_corpus_changed_restart'):search_passages(query='prix',limit=1,cursor=first['next_cursor'])
    def test_source_revocation_invalidates_cursor(self):
        doc,_=self.index('<p>'+('prix exemple '*10)+'</p><p>'+('prix autre '*10)+'</p>')
        first=search_passages(query='prix',limit=1);doc.quarantined=True;doc.save()
        with self.assertRaisesMessage(ValidationError,'passage_corpus_changed_restart'):search_passages(query='prix',limit=1,cursor=first['next_cursor'])
        self.assertEqual(search_passages(query='prix')['results'],[])
    def test_forged_cursor_rejected(self):
        with self.assertRaisesMessage(ValidationError,'passage_cursor_invalid'):search_passages(query='prix',cursor='forged')
