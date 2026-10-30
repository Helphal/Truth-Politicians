import json,subprocess,sys,unittest
from pathlib import Path

class SQLiteProcessesTests(unittest.TestCase):
    def test_real_process_lock_interruption_and_stale_review(self):
        root=Path(__file__).resolve().parent.parent
        run=subprocess.run([sys.executable,str(root/'scripts/verify_sqlite_serialization.py')],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(run.returncode,0,run.stderr)
        result=json.loads(run.stdout);self.assertTrue(result['distinct_processes']);self.assertFalse(result['multi_writer_claim'])
        self.assertTrue(result['stale_review_after_commit_refused'])
