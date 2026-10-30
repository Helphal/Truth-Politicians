"""SYNTH credentials are fixtures; verification never needs their SQL column."""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from atelier.services import verification as v
from atelier.tests import test_verification as fixtures


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class VerificationIdentityQueryTests(TestCase):
    setUp = fixtures.VerificationTests.setUp
    findings = fixtures.VerificationTests.findings
    result = fixtures.VerificationTests.result

    def test_live_identity_guard_and_positive_gate_never_fetch_password_column(self):
        # Every model/table is real on the ephemeral TestCase DB; no guard mocks.
        # Fixture creation is outside the capture, just as authentication is
        # outside the documentary verification operation.
        with CaptureQueriesContext(connection) as captured:
            user = get_user_model().objects.defer('password').get(pk=self.user.pk)
            profile = v.require_verifier(user)
            self.assertTrue(profile.is_account_usable())
            self.assertIn('password', profile.user.get_deferred_fields())
            attempt = v.start(self.job.pk, user=user, execution_id='SYNTH:no-secret-query', provider='synthetic')
            v.record_evidence(attempt.pk, user=user, findings=self.findings())
            receipt = v.finish(attempt.pk, user=user, result=self.result())
            allowed, reason = v.gate(self.revision)
        self.assertEqual(receipt.decision, 'supported')
        self.assertTrue(allowed); self.assertEqual(reason, 'supported_in_verified_scope')
        reads = [query['sql'].lower() for query in captured if 'select' in query['sql'].lower()
                 and 'auth_user' in query['sql'].lower()]
        self.assertGreaterEqual(len(reads), 3)
        # Do not print captured SQL on failure: only the stable assertion label.
        self.assertFalse(any('"auth_user"."password"' in sql for sql in reads),
                         'verification_loaded_auth_user_password_column')
