import importlib.util
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from django.conf import settings
from django.test import SimpleTestCase

class NightGuardTests(SimpleTestCase):
    def test_failed_network_probe_stops_before_any_test(self):
        path=Path(settings.BASE_DIR)/'scripts/verify_counter_review_002.py'
        spec=importlib.util.spec_from_file_location('night_guard_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with TemporaryDirectory() as tmp,patch.object(module.sys,'argv',['runner','--project-root',str(settings.BASE_DIR),'--output',tmp]),patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='SYNTHETIC GUARD FAILURE')) as run:
            with self.assertRaises(SystemExit),redirect_stdout(StringIO()):module.main()
            self.assertEqual(run.call_count,1)
            summary=json.loads((Path(tmp)/'resultats.json').read_text())
            self.assertFalse(summary['network_guard_active']);self.assertFalse(summary['passed'])
