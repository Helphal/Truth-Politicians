import hashlib,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from export_delta_v1 import records

class DeltaSafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup);self.root=Path(self.folder.name)
    def manifest(self,name,items):
        folder=self.root/name;folder.mkdir()
        raw=json.dumps({'records':items}).encode();(folder/'batch.json').write_bytes(raw)
        value={'schema':'dv-public-export-v1','created_at':'2026-09-22T00:00:00Z','source_receipts':[],
            'batches':[{'file':'batch.json','type':'person','sha256':hashlib.sha256(raw).hexdigest()}]}
        path=folder/'MANIFEST.json';path.write_text(json.dumps(value));return path
    def test_partial_snapshot_never_withdraws_and_correction_pins_previous_revision(self):
        original={'record_type':'person','record_id':'TEST-A','revision':'r1','operation':'upsert','data':{'display_name':'SYNTH A'}}
        omitted=dict(original,record_id='TEST-B')
        changed=dict(original,revision='r2',data={'display_name':'SYNTH corrected'})
        before=self.manifest('before',[original,omitted]);after=self.manifest('after',[changed]);output=self.root/'delta'
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('export_delta_v1.py')),'--before',str(before),'--after',str(after),'--output',str(output)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        manifest=json.loads((output/'MANIFEST.json').read_text());self.assertEqual(manifest['automatic_withdrawals'],0)
        self.assertEqual(manifest['omitted_baseline_records_not_withdrawn'],1)
        batch=json.loads((output/manifest['batches'][0]['file']).read_text())
        self.assertEqual(batch['records'][0]['expects_revision'],'r1')
        self.assertEqual(batch['records'][0]['operation'],'upsert')
    def test_tampered_file_is_refused(self):
        path=self.manifest('before',[]);(path.parent/'batch.json').write_text('{"records":["changed"]}')
        with self.assertRaisesRegex(ValueError,'batch_hash_mismatch'):records(path)
if __name__=='__main__':unittest.main()
