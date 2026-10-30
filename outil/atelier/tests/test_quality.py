# Copie publique : les noms de cette fixture sont fictifs.
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from atelier.models import JobRun, LoginThrottle, Source
from atelier.services.analysis import ProviderBlocked, prepare_documentary, prepare_with_external_provider
from atelier.services.analysis_provider import AnalysisProviderResult
from atelier.services.canonical import CanonicalizationError, canonical_bytes, sha256_digest
from atelier.services.contracts import (
    event_schema,
    publication_schema,
    validate_contract_envelope,
    validate_contract_schema,
    validate_event_packet_schema,
)
from atelier.services.exporter import export_outbox
from atelier.services.parsers.senate import find_nominative_vote, require_exact_quote
from atelier.services.publication import authorize_and_queue, packet_for_event

from .support import AtelierTestCase


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QualityTests(AtelierTestCase):
    def test_documentary_adapter_is_deterministic_cached_zero_cost_and_has_no_paid_fallback(self):
        candidate = {
            "draft": {"result": "inconclusive", "short_answer": "Pièces à examiner."},
            "evidence_refs": ["SRC:1@v1"],
            "method": "DV-M0.1",
        }
        first = prepare_documentary(batch_id="quality-documentary", candidate=candidate)
        second = prepare_documentary(batch_id="quality-documentary", candidate=candidate)
        self.assertEqual(first.proposal, candidate["draft"])
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.provider_calls, 0)
        self.assertEqual(first.cost_eur, 0)

        with self.assertRaisesMessage(ProviderBlocked, "analysis_provider_auth_missing"):
            prepare_with_external_provider(batch_id="quality-external", candidate=candidate)
        blocked = JobRun.objects.get(task_name="editorial_preparation_external")
        self.assertEqual(blocked.state, JobRun.State.BLOCKED)
        self.assertEqual(blocked.cost_units, 0)
        self.assertEqual(blocked.configuration["fallback"], "none")
        self.assertEqual(blocked.configuration["blocked_reason"], "analysis_provider_auth_missing")

    @override_settings(DV_ANALYSIS_PROVIDER_ENDPOINT="http://192.0.2.127:8000", DV_ANALYSIS_PROVIDER_TOKEN="configured-in-test")
    @patch("atelier.services.analysis.prepare_with_analysis_provider")
    def test_analysis_provider_preparation_is_cached_and_never_creates_review_or_publication(self, mock_prepare):
        candidate = {"declaration": "Source publique exacte.", "evidence_refs": ["SRC:REAL@v1"]}
        proposal = {
            "result": "inconclusive",
            "short_answer": "Revue humaine requise.",
            "main_reservation": "Périmètre incomplet.",
            "reasoning": "Proposition documentaire uniquement.",
            "included_acts": [{"stable_id": "vote-1"}],
            "excluded_acts": [{"stable_id": "vote-2", "reason": "hors périmètre"}],
        }
        mock_prepare.return_value = AnalysisProviderResult(
            proposal=proposal,
            model="local-model",
            provider="local",
            usage={"output_tokens": 42},
            cost_eur=None,
        )
        first = prepare_with_external_provider(batch_id="quality-analysis_provider", candidate=candidate)
        second = prepare_with_external_provider(batch_id="quality-analysis_provider", candidate=candidate)
        self.assertEqual(first.proposal, proposal)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.provider_calls, 1)
        self.assertEqual(second.provider_calls, 0)
        self.assertIsNone(first.cost_eur)
        self.assertEqual(mock_prepare.call_count, 1)
        job = JobRun.objects.get(task_name="editorial_preparation_external", batch_id="quality-analysis_provider")
        self.assertFalse(job.configuration["human_review_created"])
        self.assertFalse(job.configuration["publication_authorized"])
        self.assertFalse(job.configuration["cost_reported"])

    def test_senate_html_extracts_only_explicit_nominative_vote_and_exact_quote(self):
        html = """
        <section><ul>
          <li><b>Pour</b> : Mmes Alice Exemple, Béatrice Exemple.</li>
          <li><b>Contre</b> : M. Exemple.</li>
        </ul></section>
        <p>Je l&#8217;ai annoncé d&#8217;emblée : je voterai cette proposition de loi.</p>
        """.encode("utf-8")
        self.assertEqual(
            find_nominative_vote(html, "Béatrice Exemple"),
            {"source_category": "Pour", "normalized_category": "for"},
        )
        self.assertIsNone(find_nominative_vote(html, "Personne absente"))
        require_exact_quote(html, "Je l’ai annoncé d’emblée : je voterai cette proposition de loi.")
        with self.assertRaisesMessage(ValueError, "senat_exact_quote_not_found"):
            require_exact_quote(html, "Citation inventée.")

    def test_private_settings_require_https_and_proxy_header_is_opt_in(self):
        probe = (
            "import json; import dv_project.settings as s; "
            "print(json.dumps({'redirect': s.SECURE_SSL_REDIRECT, "
            "'proxy': getattr(s, 'SECURE_PROXY_SSL_HEADER', None), "
            "'hsts': s.SECURE_HSTS_SECONDS}))"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DV_ENV": "private",
                "DV_SECRET_KEY": "x" * 60,
                "DV_REVIEW_ATTESTATION_KEY": "y" * 32,
                "DV_ALLOWED_HOSTS": "private.example.invalid",
                "DV_NETWORK_MODE": "disabled",
                "DV_TRUST_PROXY_SSL_HEADER": "0",
            }
        )
        disabled = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        disabled_settings = json.loads(disabled.stdout)
        self.assertTrue(disabled_settings["redirect"])
        self.assertEqual(disabled_settings["hsts"], 31_536_000)
        self.assertIsNone(disabled_settings["proxy"])

        environment["DV_TRUST_PROXY_SSL_HEADER"] = "1"
        enabled = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            json.loads(enabled.stdout)["proxy"],
            ["HTTP_X_FORWARDED_PROTO", "https"],
        )

    def test_canonicalization_vector_is_stable_and_float_is_rejected(self):
        value = {"z": [True, None, 3], "é": "texte", "a": {"b": "c"}}
        expected = '{"a":{"b":"c"},"z":[true,null,3],"é":"texte"}'.encode("utf-8")
        self.assertEqual(canonical_bytes(value), expected)
        self.assertEqual(sha256_digest(value), "2cebc1ad6b4fdd6628f577492c7cce028d23f0924a7451bf6552191524c99bd9")
        with self.assertRaisesMessage(CanonicalizationError, "floating_number_forbidden"):
            canonical_bytes({"score": 0.5})

    def test_contract_meta_schema_is_valid_and_root_is_closed(self):
        validate_contract_schema()
        self.assertFalse(publication_schema()["additionalProperties"])
        self.assertFalse(event_schema()["additionalProperties"])

    def test_draft2_examples_are_synthetic_valid_and_digest_consistent(self):
        examples = sorted((Path(settings.BASE_DIR) / "contracts" / "examples").glob("*.json"))
        self.assertEqual(len(examples), 3)
        self.assertEqual(
            {path.stem for path in examples},
            {"official-vote.synthetic", "speech-vote.synthetic", "vote-comparison.synthetic"},
        )
        for path in examples:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            validate_contract_envelope(envelope)
            self.assertEqual(envelope["data_scope"], "synthetic")
            self.assertEqual(envelope["authorization"]["snapshot_digest"], sha256_digest(envelope["public"]))

    def test_event_schema_accepts_signed_packet_and_rejects_unknown_field(self):
        _, revision, _ = self.make_revision()
        packet = packet_for_event(authorize_and_queue(revision, "test"))
        validate_event_packet_schema(packet)
        packet["unexpected"] = True
        with self.assertRaisesMessage(ValidationError, "event_contract_invalid"):
            validate_event_packet_schema(packet)

    def test_login_throttle_uses_digest_and_locks_after_five_failures(self):
        client = Client()
        remote = "192.0.2.44"
        username = "nobody"
        for _ in range(5):
            response = client.post(
                reverse("login"),
                {"username": username, "password": "incorrect-long-password"},
                REMOTE_ADDR=remote,
            )
            self.assertEqual(response.status_code, 200)
        throttled = client.post(
            reverse("login"),
            {"username": username, "password": "incorrect-long-password"},
            REMOTE_ADDR=remote,
        )
        self.assertEqual(throttled.status_code, 429)
        from atelier.services.login_throttle import account_throttle_digest

        attempt = LoginThrottle.objects.get(key_digest=account_throttle_digest(username))
        self.assertEqual(len(attempt.key_digest), 64)
        self.assertNotIn(remote, attempt.key_digest)
        self.assertIsNotNone(attempt.locked_until)
        for digest in LoginThrottle.objects.values_list("key_digest", flat=True):
            self.assertNotIn(remote, digest)
            self.assertEqual(len(digest), 64)

    def test_outbox_export_is_bounded_signed_and_non_mutating(self):
        _, revision, _ = self.make_revision()
        event = authorize_and_queue(revision, "test")
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name) / "batch"
            manifest = export_outbox(destination, environment="test", limit=1)
            self.assertEqual(manifest["count"], 1)
            packet = json.loads((destination / manifest["items"][0]["file"]).read_text(encoding="utf-8"))
            self.assertEqual(packet["signature_algorithm"], "HMAC-SHA256")
            self.assertEqual(packet["event_id"], str(event.event_id))
            self.assertEqual(event.state, "pending")

    def test_source_catalog_refresh_preserves_observed_operational_state(self):
        source = Source.objects.create(
            stable_id="AN_VOTES_XVI",
            producer="Assemblée nationale",
            family="scrutins_archive",
            discovery_url="https://data.assemblee-nationale.fr/archives-16e/votes",
            document_url="https://data.assemblee-nationale.fr/example.zip",
            access_status=Source.AccessStatus.ACTIVE,
            rights_status=Source.RightsStatus.OPEN,
            operational_enabled=True,
            last_success_at=timezone.now(),
        )
        call_command("seed_sources", stdout=StringIO())
        source.refresh_from_db()
        self.assertEqual(source.access_status, Source.AccessStatus.ACTIVE)
        self.assertIsNotNone(source.last_success_at)
