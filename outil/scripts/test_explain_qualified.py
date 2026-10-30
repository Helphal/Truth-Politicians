import copy, hashlib, json, os, tempfile, unittest
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dv_project.settings')
import django
django.setup()
from atelier.services.explain.qualified import export_qualified
from atelier.services.explain.contract_v02 import digest


class QualifiedExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dv-qualified-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        text='SYNTH institutional source\n'
        self.hash=hashlib.sha256(text.encode()).hexdigest()
        (self.root/'source.txt').write_text(text,encoding='utf-8')
        (self.root/'source.receipt.json').write_text(json.dumps({'http_status':200,'text_sha256':self.hash,
            'final_url':'https://www.senat.fr/synth','retrieved_at':'2026-09-27T00:00:00Z','publisher':'SYNTH Sénat'}),encoding='utf-8')
        e=dict(stable_id='explain.synth.one',slug='synth-one',revision='r1',expects_revision='',operation='upsert',title='SYNTH',body='SYNTH assertion',scope='SYNTH',reserve='SYNTH',credit='SYNTH',verified_on='2026-09-27',qualification='qualified_documentary',assertions=[{'text':'SYNTH assertion','source_indexes':[0]}],sources=[dict(url='https://www.senat.fr/synth',publisher='SYNTH Sénat',locator='SYNTH paragraph',consulted_on='2026-09-27',text_sha256=self.hash)])
        e['content_sha256']=digest(e)
        self.payload=dict(format='dv.explain-public/0.2.0',corpus='test',partisan_commentary=False,entries=[e])

    def test_bound_source_is_required_and_original_is_not_changed(self):
        original=copy.deepcopy(self.payload)
        self.assertEqual(export_qualified(self.payload,self.root),original)
        (self.root/'source.txt').write_text('altered',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'consultation'):export_qualified(self.payload,self.root)
        self.assertEqual(self.payload,original)

    def test_draft_flag_and_unbound_sentence_cannot_be_promoted(self):
        for field,value in [('qualification','draft'),('body','added unsupported sentence')]:
            p=copy.deepcopy(self.payload);p['entries'][0][field]=value
            p['entries'][0]['content_sha256']=digest(p['entries'][0])
            with self.assertRaises(ValueError):export_qualified(p,self.root)

    def test_all_qualification_sources_must_have_matching_consultation(self):
        p=copy.deepcopy(self.payload);p['entries'][0]['sources'][0]['consulted_on']='2026-09-26'
        p['entries'][0]['content_sha256']=digest(p['entries'][0])
        with self.assertRaisesRegex(ValueError,'consultation'):export_qualified(p,self.root)
