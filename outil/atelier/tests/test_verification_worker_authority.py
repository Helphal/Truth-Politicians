"""Authority regression: historical commands cannot restart exhausted calls."""
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

class HistoricalWorkerAuthorityTests(SimpleTestCase):
    def test_old_job_instruction_refused_before_any_cli_or_database(self):
        with patch("atelier.management.commands.run_verification_worker.CodexCLI") as cli:
            with self.assertRaisesMessage(CommandError,"legacy_execution_closed_use_versioned_plan_and_allocation"):
                call_command("run_verification_worker",job=42,user_id=1,execution_id="old-START",output="unused")
            cli.assert_not_called()

    def test_missing_old_arguments_are_not_a_reason_to_start_cli(self):
        with patch("atelier.management.commands.run_verification_worker.CodexCLI") as cli:
            with self.assertRaises(CommandError):
                call_command("run_verification_worker")
            cli.assert_not_called()

    def test_legacy_service_without_runner_refuses_before_authority_or_start(self):
        from atelier.services.verification_executor import execute_job, ExecutionFailure
        with patch("atelier.services.verification_executor.v.start") as start, patch("atelier.services.verification_executor.CodexCLI") as cli:
            with self.assertRaises(ExecutionFailure):
                execute_job(42,user=None,execution_id="old-START",output_root="unused")
            start.assert_not_called()
            cli.assert_not_called()
