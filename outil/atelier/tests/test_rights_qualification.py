"""SYNTH files with patched approved hashes; no real qualification by JSON."""
from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from atelier.models import DocumentVersion, Source
from atelier.services.canonical import sha256_digest
from atelier.services import rights_qualification as rights


class RightsRuntimeBoundaryTests(SimpleTestCase):
    def test_full_runtime_settings_honour_exact_conditions_and_plan_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = {name: os.environ[name] for name in ('SystemRoot','WINDIR','Path','TEMP','TMP','COMSPEC','PATHEXT') if name in os.environ}
            env.update(DV_ENV='test', DV_SECRET_KEY='SYNTH_FULL_RUNTIME_CONFIGURATION_ONLY',
                DV_RUNTIME_DIR=str(root / 'runtime-candidate'), DV_DATABASE_PATH=':memory:',
                DV_RIGHTS_EVIDENCE_DIR=str(root / 'droits/conditions'),
                DV_VERIFICATION_PLAN_ROOT=str(root / 'inference/runtime'))
            code = ('from dv_project import settings; import json; '
                'print(json.dumps([str(settings.DV_RIGHTS_EVIDENCE_DIR),str(settings.DV_VERIFICATION_PLAN_ROOT)]))')
            result = subprocess.run([sys.executable,'-c',code], cwd=Path(__file__).resolve().parents[2],
                env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, 'closed full runtime configuration failed')
            self.assertEqual(json.loads(result.stdout), [str(root / 'droits/conditions'), str(root / 'inference/runtime')])

    def test_real_runtime_setting_fallback_without_fixture_only_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / 'runtime-candidate'
            expected = Path(folder) / 'droits' / 'conditions'
            with override_settings(DV_RIGHTS_EVIDENCE_DIR=None, DV_RUNTIME_DIR=None, RUNTIME_DIR=runtime):
                self.assertEqual(rights._evidence_root(), expected.resolve())
            with override_settings(DV_RIGHTS_EVIDENCE_DIR=expected, DV_RUNTIME_DIR=None, RUNTIME_DIR=None):
                self.assertEqual(rights._evidence_root(), expected.resolve())

    def test_missing_runtime_and_conditions_configuration_fails_closed(self):
        with override_settings(DV_RIGHTS_EVIDENCE_DIR=None, DV_RUNTIME_DIR=None, RUNTIME_DIR=None):
            with self.assertRaisesMessage(ValidationError, 'rights_conditions_runtime_required'):
                rights._evidence_root()


@override_settings(DV_ENV='test')
class RightsQualificationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.vault = self.directory / 'runtime-candidate' / 'evidence-vault'
        self.conditions = self.directory / 'droits' / 'conditions'
        self.vault.mkdir(parents=True)
        self.conditions.mkdir(parents=True)
        data = b'<html><h1>SYNTH parliamentary text</h1><p>SYNTH exact text.</p></html>'
        self.source = Source.objects.create(stable_id='SYNTH-SENATE-SOURCE', producer='SYNTH',
            family='SYNTH', discovery_url='https://www.senat.fr/',
            document_url='https://www.senat.fr/amendements/2025-2026/298/Amdt_13.html')
        self.digest = hashlib.sha256(data).hexdigest()
        self.original = self.vault / (self.digest + '.html')
        self.original.write_bytes(data)
        self.doc = DocumentVersion.objects.create(source=self.source, version_id='SYNTH-1',
            sha256=self.digest, byte_size=len(data), media_type='text/html',
            original_path=str(self.original), captured_at=datetime(2026, 10, 3, tzinfo=dt_timezone.utc))
        self.ref = {'source_id': self.source.stable_id, 'version_id': self.doc.version_id,
            'sha256': self.digest, 'public_url': self.source.document_url}
        self.snapshot = {'scope': 'synthetic', 'content': {'title': 'SYNTH'},
            'sources': [{'reference': self.ref, 'state': 'available',
                         'captured_at': '2026-10-03T00:00:00+00:00', 'document_date': None}]}
        raw_conditions = b'<html><p>SYNTH conditions for a test fixture, not real rights.</p></html>'
        digest = hashlib.sha256(raw_conditions).hexdigest()
        self.condition_file = self.conditions / (digest + '.html')
        self.condition_file.write_bytes(raw_conditions)
        url = 'https://www.senat.fr/mentions-legales.html'
        self.manifest = {'contract': 'dv.primary-conditions-collection/1',
            'credentials_used': 0, 'cookies_saved': 0, 'rows': [{
                'key': 'senate_text_conditions', 'requested_url': url, 'url': url,
                'captured_at': '2026-10-04T08:00:00+00:00', 'sha256': digest,
                'filename': self.condition_file.name, 'byte_size': len(raw_conditions),
                'media_type': 'text/html', 'http_status': 200, 'status': 'received'}]}
        self.manifest_path = self.conditions / 'RECU-COLLECTE-CONDITIONS.json'
        self.write_manifest()
        approved = dict(rights._APPROVED_CONDITIONS)
        approved['senate_text_conditions'] = (url, digest)
        guard = patch.object(rights, '_APPROVED_CONDITIONS', approved)
        guard.start()
        self.addCleanup(guard.stop)
        settings_guard = override_settings(DV_EVIDENCE_VAULT_PATH=self.vault,
            DV_RIGHTS_EVIDENCE_DIR=self.conditions, DV_RUNTIME_DIR=self.vault.parent)
        settings_guard.enable()
        self.addCleanup(settings_guard.disable)

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def qualification(self):
        return rights.build_source_qualification(self.snapshot, self.ref, as_of='2026-10-04')

    def report(self, mode='private_review'):
        return rights.build_rights_report(self.snapshot, [self.qualification()],
            render_mode=mode, as_of='2026-10-04')

    def test_seven_usages_are_closed_and_private_text_binding_is_recomputed(self):
        qualification = self.qualification()
        self.assertEqual([u['usage'] for u in qualification['usages']], list(rights.USAGES))
        report = self.report()
        self.assertEqual(report['usages'][0]['status'], 'compatible')
        self.assertEqual(report['render_profile'], 'private_text_reader/1')
        self.assertIn('senate_urls_plain_text_only', report['render_requirements'])
        self.assertEqual(rights.validate_rights_report(self.snapshot, report,
            source_root=self.vault, as_of='2026-10-04'), report)
        self.assertEqual(rights.validate_rights_report(self.snapshot, report,
            as_of='2026-10-05'), report)

    def test_active_senate_link_and_capture_do_not_inherit_private_qualification(self):
        usages = {item['usage']: item for item in self.qualification()['usages']}
        self.assertEqual(usages['official_link']['status'], 'requires_authorization')
        self.assertEqual(usages['capture']['status'], 'requires_authorization')
        for mode in ('official_link', 'public_excerpt', 'public_long_reproduction'):
            self.assertEqual(self.report(mode)['usages'][0]['status'], 'unqualified')

    def test_forged_compatible_legacy_json_is_refused(self):
        report = self.report()
        report.pop('source_qualifications')
        report['rights_digest'] = sha256_digest({k: v for k, v in report.items() if k != 'rights_digest'})
        with self.assertRaisesMessage(ValidationError, 'rights_recomputed_source_qualifications_required'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_forged_usage_even_with_new_digest_is_refused(self):
        report = self.report('official_link')
        report['usages'][0]['status'] = 'compatible'
        report['rights_digest'] = sha256_digest({k: v for k, v in report.items() if k != 'rights_digest'})
        with self.assertRaisesMessage(ValidationError, 'rights_report_not_recomputed'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_forged_qualification_and_digest_are_refused(self):
        qualification = self.qualification()
        qualification['usages'][0]['status'] = 'qualified'
        qualification['qualification_digest'] = sha256_digest({k: v for k, v in qualification.items() if k != 'qualification_digest'})
        with self.assertRaisesMessage(ValidationError, 'rights_source_qualification_not_recomputed'):
            rights.validate_source_qualification(self.snapshot, qualification, as_of='2026-10-04')

    def test_original_changed_is_refused(self):
        report = self.report()
        self.original.write_bytes(b'SYNTH altered original')
        with self.assertRaisesMessage(ValidationError, 'rights_received_source_integrity_failed'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_primary_conditions_changed_is_refused(self):
        report = self.report()
        self.condition_file.write_bytes(b'SYNTH altered conditions')
        with self.assertRaisesMessage(ValidationError, 'rights_conditions_sha_changed'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_manifest_fake_url_and_hash_are_refused(self):
        for field, value in [('url', 'https://example.org/fake'), ('sha256', 'a' * 64),
                             ('http_status', 404), ('requested_url', 'https://example.org/fake')]:
            original = self.manifest['rows'][0][field]
            self.manifest['rows'][0][field] = value
            self.write_manifest()
            with self.assertRaisesMessage(ValidationError, 'rights_primary_conditions_not_approved'):
                self.qualification()
            self.manifest['rows'][0][field] = original
        self.write_manifest()

    def test_conditions_expire_and_cannot_be_backdated(self):
        report = self.report()
        with self.assertRaisesMessage(ValidationError, 'rights_source_qualification_expired'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-12')
        with self.assertRaisesMessage(ValidationError, 'rights_conditions_not_yet_received'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-03')

    def test_source_url_and_version_change_are_refused(self):
        report = self.report()
        changed = deepcopy(self.snapshot)
        changed['sources'][0]['reference']['version_id'] = 'SYNTH-2'
        with self.assertRaises(ValidationError):
            rights.validate_rights_report(changed, report, as_of='2026-10-04')
        self.source.document_url = 'https://www.senat.fr/leg/ppl25-298.html'
        self.source.save(update_fields=['document_url'])
        with self.assertRaisesMessage(ValidationError, 'rights_received_source_identity_changed'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_missing_version_qualification_and_added_field_are_refused(self):
        with self.assertRaisesMessage(ValidationError, 'rights_all_exact_source_qualifications_required'):
            rights.build_rights_report(self.snapshot, [], as_of='2026-10-04')
        report = self.report()
        report['usages'][0]['publisher_name'] = 'SYNTH'
        report['rights_digest'] = sha256_digest({k: v for k, v in report.items() if k != 'rights_digest'})
        with self.assertRaisesMessage(ValidationError, 'rights_report_not_recomputed'):
            rights.validate_rights_report(self.snapshot, report, as_of='2026-10-04')

    def test_html_never_inherits_open_data_and_archive_personal_data_remain_unknown(self):
        resource, keys, _ = rights._resource(self.ref, 'text/html')
        self.assertEqual(resource, 'parliamentary_text')
        self.assertNotIn('senate_open_data_conditions', keys)
        archive_usage = rights._usage('private_excerpt', 'institutional_open_data_archive', 'Assemblée nationale')
        self.assertEqual(archive_usage['status'], 'unknown')
        self.assertIn('personal_data_reuse_basis_not_assessed', archive_usage['limits'])

    def test_mime_charset_is_parameters_not_another_resource(self):
        for media in ('text/html', 'text/html; charset=UTF-8', 'Text/HTML ; charset=utf-8'):
            resource, keys, _ = rights._resource(self.ref, media)
            self.assertEqual(resource, 'parliamentary_text')
            self.assertEqual(keys, ('senate_text_conditions',))
