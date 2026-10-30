"""SYNTH subprocess failures; no CLI, provider, authentication or real DB."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from atelier.services import verification_executor as x
from atelier.services.verification_plan_executor import ProtocolCLI
from atelier.tests.test_verification_protocol import plan_fixture
from atelier.services.verification_protocol import packet_for


class CliDiagnosticTests(SimpleTestCase):
    def run_failed_cli(self, stderr, events=None):
        plan=plan_fixture(); packet=packet_for(plan,plan['batches'][0])
        runner=object.__new__(ProtocolCLI); runner.executable='SYNTH-codex'
        runner.timeout=600
        runner.identity={'cli':'codex-cli 0.159.1','model':'SYNTH','model_reasoning_effort':'none',
                         'model_offered_by_cli':True}
        stdout=b'\n'.join(json.dumps(event).encode('utf-8') for event in
            (events or [{'type':'thread.started','thread_id':'SYNTH-thread'}]))+b'\n'
        with tempfile.TemporaryDirectory(prefix='dv-SYNTH-diagnostic-') as tmp:
            directory=Path(tmp)
            with patch.object(runner,'_popen',return_value=SimpleNamespace(returncode=1)) as launch:
                with patch.object(x,'_communicate',return_value=(stdout,stderr)):
                    with self.assertRaises(x.ExecutionFailure) as raised:
                        runner.run('evidence',packet,directory,lambda:None)
            self.assertEqual(launch.call_count,1)
            metadata=json.loads((directory/'execution.json').read_bytes())
            files={path.name:path.read_bytes() for path in directory.iterdir() if path.is_file()}
            return raised.exception.code,metadata,files

    def test_thread_started_only_then_input_limit_is_classified_from_stderr(self):
        stderr=(b"Error: Invalid 'input[0].content[0].text': string too long. "
                b'Expected a string with maximum length 1048576, but got length 1103110. SYNTH')
        code,metadata,files=self.run_failed_cli(stderr)
        self.assertEqual(code,'provider_input_text_size_limit')
        diagnostic=metadata['failure_diagnostic']
        self.assertEqual(diagnostic['category'],code)
        self.assertEqual(diagnostic['stderr_bytes'],len(stderr))
        self.assertEqual(diagnostic['stderr_sha256'],hashlib.sha256(stderr).hexdigest())
        self.assertIn('input_text_length_message',diagnostic['matched_pattern_ids'])
        self.assertEqual(metadata['errors'],[]); self.assertEqual(metadata['tool_events'],0)
        self.assertEqual(metadata['event_types'],['thread.started/'])
        self.assertIsNone(metadata['output_sha256'])
        self.assertNotIn(stderr,b'\n'.join(files.values()))
        self.assertFalse(diagnostic['text_persisted'])

    def test_context_schema_environment_quota_and_model_signatures_are_closed(self):
        cases=[(b'SYNTH context_length_exceeded','provider_context_length_exceeded'),
               (b'Your input exceeds the context window of this model. SYNTH','provider_context_length_exceeded'),
               (b"Invalid schema for response_format 'SYNTH': string too long. Expected maximum length 20.", 'provider_schema_refused'),
               (b'SYNTH invalid_json_schema','provider_schema_refused'),
               (b'SYNTH failed to spawn worker: os error 206','execution_environment_or_os_limit'),
               (b'SYNTH The filename or extension is too long','execution_environment_or_os_limit'),
               (b'SYNTH rate limit exceeded','quota_or_rate_limit'),
               (b'SYNTH insufficient_quota','quota_or_rate_limit'),
               (b'SYNTH model is not supported by this auth','configured_model_not_supported_by_existing_auth')]
        for stderr,expected in cases:
            with self.subTest(expected=expected):
                result=x._closed_cli_diagnostic(stderr,[],returncode=1)
                self.assertEqual(result['category'],expected)
                self.assertTrue(result['matched_pattern_ids'])
                self.assertFalse(result['text_persisted'])

    def test_unknown_diagnostic_retains_only_hash_size_and_closed_values(self):
        sentinel='SYNTH_PRIVATE_VALUE_DO_NOT_STORE'
        stderr=f'SYNTH unknown startup trouble password={sentinel} https://example.invalid/private Bearer {sentinel}'.encode()
        code,metadata,files=self.run_failed_cli(stderr)
        self.assertEqual(code,'provider_unavailable')
        self.assertEqual(metadata['failure_diagnostic']['matched_pattern_ids'],[])
        self.assertEqual(metadata['failure_diagnostic']['stderr_bytes'],len(stderr))
        data=b'\n'.join(files.values())
        self.assertNotIn(sentinel.encode(),data); self.assertNotIn(b'example.invalid/private',data)
        self.assertNotIn(stderr,data)

    def test_explicit_error_event_is_classified_but_model_prose_and_credentials_are_not_saved(self):
        sentinel='SYNTH_SECRET_ERROR_EVENT'
        events=[{'type':'thread.started','thread_id':'SYNTH-thread'},
            {'type':'item.completed','item':{'type':'reasoning','text':'SYNTH quota exceeded model is not supported'}},
            {'type':'item.completed','item':{'type':'agent_message','text':'SYNTH context_length_exceeded'}},
            {'type':'error','message':'SYNTH invalid_json_schema Bearer '+sentinel},
            {'type':'turn.failed','error':{'message':'SYNTH schema_refused password='+sentinel}}]
        code,metadata,files=self.run_failed_cli(b'',events)
        self.assertEqual(code,'provider_schema_refused')
        self.assertEqual(metadata['failure_diagnostic']['explicit_error_events'],2)
        self.assertEqual(metadata['errors'],['provider_schema_refused','provider_schema_refused'])
        self.assertNotIn(sentinel.encode(),b'\n'.join(files.values()))
        self.assertNotIn('model_not_supported',metadata['failure_diagnostic']['matched_pattern_ids'])

    def test_model_prose_alone_cannot_supply_infrastructure_diagnostic(self):
        events=[{'type':'thread.started','thread_id':'SYNTH-thread'},
                {'type':'item.completed','item':{'type':'agent_message','text':'SYNTH model is not supported. rate limit exceeded.'}},
                {'type':'item.completed','item':{'type':'reasoning','text':'SYNTH invalid_json_schema context_length_exceeded'}}]
        code,metadata,_=self.run_failed_cli(b'SYNTH unknown infrastructure condition',events)
        self.assertEqual(code,'provider_unavailable')
        self.assertEqual(metadata['failure_diagnostic']['explicit_error_events'],0)
        self.assertEqual(metadata['failure_diagnostic']['matched_pattern_ids'],[])

    def test_successful_process_is_not_labeled_failed_and_ansi_does_not_hide_signature(self):
        self.assertEqual(x._closed_cli_diagnostic(b'SYNTH rate limit exceeded',[],returncode=0)['category'],'none')
        result=x._closed_cli_diagnostic(b'\x1b[31mError:\x1b[0m SYNTH input_text string too long. Expected maximum length 1048576.',[],returncode=1)
        self.assertEqual(result['category'],'provider_input_text_size_limit')

    def test_native_input_limit_signatures_are_closed_without_stderr_storage(self):
        cases=[b'SYNTH Input exceeds the maximum length of 1048576 characters.',
               b'SYNTH queued user input exceeds the maximum length of 1000000 characters (1069259 provided)',
               b'SYNTH {"input_error_code":"input_too_large"}']
        for stderr in cases:
            with self.subTest(form=cases.index(stderr)):
                code,metadata,files=self.run_failed_cli(stderr)
                self.assertEqual(code,'provider_input_text_size_limit')
                self.assertTrue(any(name.startswith('native_input_') for name in
                    metadata['failure_diagnostic']['matched_pattern_ids']))
                self.assertNotIn(stderr,b'\n'.join(files.values()))

    def test_native_input_limit_in_normal_model_output_cannot_classify_failure(self):
        events=[{'type':'thread.started','thread_id':'SYNTH-thread'},
            {'type':'item.completed','item':{'type':'agent_message',
             'text':'SYNTH Input exceeds the maximum length of 1048576 characters.'}},
            {'type':'item.completed','item':{'type':'reasoning',
             'text':'SYNTH input_too_large'}}]
        code,metadata,_=self.run_failed_cli(b'SYNTH unknown infrastructure error',events)
        self.assertEqual(code,'provider_unavailable')
        self.assertEqual(metadata['failure_diagnostic']['matched_pattern_ids'],[])
