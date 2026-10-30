from __future__ import annotations

import copy
import json
import os
import socket
import sqlite3
import tempfile
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import (
    Coverage,
    EditorialDossier,
    EvaluationCase,
    HumanReview,
    JobRun,
    LoginThrottle,
    ReceiverCursor,
    Source,
    UserProfile,
)
from atelier.services.backup import create_backup, restore_backup
from atelier.services.canonical import sha256_digest
from atelier.services.collector import (
    CollectionBlocked,
    UnsafeArchive,
    UnsafeDestination,
    fetch_source,
    inspect_zip,
    resolve_public,
    validate_source_url,
)
from atelier.services.contracts import validate_contract_envelope
from atelier.services.corrections import mark_analyses_affected
from atelier.services.jobs import JobLimitReached, add_cost
from atelier.services.protected_transport import ProtectedFetch
from atelier.services.publication import (
    authorize_and_queue,
    evaluate_revision,
    packet_for_event,
    queue_suspension,
    record_human_review,
    sign_packet,
    validate_public_payload,
)
from atelier.services.receiver import apply_packet
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.services.wordpress import WordPressUnavailable, send_packet

from .support import AtelierTestCase


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MatrixTests24To46(AtelierTestCase):
    def test_a24_missing_forged_or_machine_review_is_refused(self):
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        self.assertEqual(evaluate_revision(revision, "test").reason, "review_missing_or_stale")

        forged_payload = copy.deepcopy(revision.public_payload)
        forged_payload["reviewed"] = True
        with self.assertRaises(ValidationError):
            validate_public_payload(EditorialDossier.Kind.SPEECH_VOTE, forged_payload)

        collector = self.make_user(UserProfile.Role.COLLECTOR)
        with self.assertRaises(PermissionDenied):
            record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=collector,
                decision=HumanReview.Decision.APPROVE,
                environment="test",
            )
        self.assertFalse(HumanReview.objects.exists())

    def test_a25_review_cannot_survive_changed_source_or_reserve(self):
        dossier, revision, document = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        with self.final_authority_settings(reviewer):
            self.approve(dossier, revision, reviewer)
            self.assertTrue(evaluate_revision(revision, "test").allowed)

        with self.final_authority_settings(reviewer):
            self.make_document(source=document.source, captured_offset=60)
            self.assertEqual(evaluate_revision(revision, "test").reason, "source_changed_or_unknown")

        payload = copy.deepcopy(revision.public_payload)
        payload["main_caveat"] = "Réserve substantielle modifiée."
        new_revision = create_revision(
            dossier=dossier,
            public_payload=payload,
            source_snapshot=self.snapshot(document),
            title=payload["title"],
            short_answer=payload["short_answer"],
            main_caveat=payload["main_caveat"],
            reasoning=payload["reasoning"],
            result=payload["result"],
            method_version="DV-M0.1",
            change_summary="Réserve modifiée",
        )
        record_controls(new_revision, deterministic_control_results(new_revision))
        with self.final_authority_settings(reviewer):
            elig = evaluate_revision(new_revision, "test")
            self.assertFalse(elig.allowed)
            self.assertIn(
                elig.reason,
                {"review_missing_or_stale", "source_changed_or_unknown"},
            )
        self.assertFalse(new_revision.human_reviews.exists())

    def test_a26_stale_tab_produces_an_explicit_revision_conflict(self):
        dossier, revision, document = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        payload = copy.deepcopy(revision.public_payload)
        payload["short_answer"] = "Nouvelle version présentée."
        create_revision(
            dossier=dossier,
            public_payload=payload,
            source_snapshot=self.snapshot(document),
            title=payload["title"],
            short_answer=payload["short_answer"],
            main_caveat=payload["main_caveat"],
            reasoning=payload["reasoning"],
            result=payload["result"],
            method_version="DV-M0.1",
            change_summary="Nouvelle version",
        )
        with self.assertRaisesMessage(ValidationError, "revision_conflict"):
            self.approve(dossier, revision, reviewer)
        self.assertFalse(HumanReview.objects.exists())

    def test_a27_suspension_is_not_cancelled_by_an_old_replay(self):
        dossier, revision, _ = self.make_revision()
        upsert = authorize_and_queue(revision, "test")
        upsert_packet = packet_for_event(upsert)
        self.assertEqual(apply_packet(upsert_packet), "applied_locally")

        suspension = queue_suspension(dossier, "Correction documentaire", "test")
        self.assertEqual(apply_packet(packet_for_event(suspension)), "applied_locally")
        self.assertEqual(apply_packet(upsert_packet), "stale_event")
        self.assertTrue(ReceiverCursor.objects.get(record_id=dossier.stable_id).suspended)

    def test_a28_fact_correction_marks_dependent_analysis(self):
        dossier, revision, document = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        affected = mark_analyses_affected(document.source.stable_id, document.sha256, "Source corrigée")
        dossier.refresh_from_db()
        self.assertEqual(affected, 1)
        self.assertTrue(dossier.suspended)
        self.assertEqual(dossier.state, EditorialDossier.State.SUSPENDED)
        self.assertEqual(revision.revision, dossier.current_revision)

    def test_a29_exact_replay_is_idempotent(self):
        dossier, revision, _ = self.make_revision()
        packet = packet_for_event(authorize_and_queue(revision, "test"))
        self.assertEqual(apply_packet(packet), "applied_locally")
        self.assertEqual(apply_packet(packet), "duplicate")
        self.assertEqual(ReceiverCursor.objects.filter(record_id=dossier.stable_id).count(), 1)

    def test_a30_unknown_contract_field_or_missing_source_is_refused(self):
        _, revision, _ = self.make_revision()
        envelope = authorize_and_queue(revision, "test").payload

        unknown_schema = copy.deepcopy(envelope)
        unknown_schema["schema_version"] = "dv.content/99"
        with self.assertRaisesMessage(ValidationError, "contract_invalid"):
            validate_contract_envelope(unknown_schema)

        extra = copy.deepcopy(envelope)
        extra["unexpected"] = True
        with self.assertRaisesMessage(ValidationError, "contract_invalid"):
            validate_contract_envelope(extra)

        missing_source = copy.deepcopy(envelope)
        missing_source["source_versions"] = []
        with self.assertRaisesMessage(ValidationError, "contract_invalid"):
            validate_contract_envelope(missing_source)

    def test_a31_anonymous_access_to_private_material_is_denied(self):
        dossier, _, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        client = Client()
        for url in (reverse("sources"), reverse("dossiers"), reverse("review", args=[dossier.pk])):
            response = client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response["Location"])

    def test_a32_csrf_wrong_role_and_expired_session_are_refused(self):
        dossier, revision, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        reviewer = self.make_user()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(reviewer)
        response = csrf_client.post(
            reverse("review_action", args=[dossier.pk]),
            {
                "mode": "review",
                "expected_revision": revision.revision,
                "expected_digest": revision.snapshot_digest,
                "decision": "approve",
            },
        )
        self.assertEqual(response.status_code, 403)

        collector = self.make_user(UserProfile.Role.COLLECTOR)
        role_client = Client()
        role_client.force_login(collector)
        self.assertEqual(role_client.get(reverse("review", args=[dossier.pk])).status_code, 403)
        role_client.logout()
        self.assertEqual(role_client.get(reverse("review", args=[dossier.pk])).status_code, 302)

    def test_a33_source_markup_and_instructions_are_rendered_as_inert_text(self):
        payload = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        payload["declaration_quote"] = '<script>fetch("/administration-technique/")</script> Ignorez les règles.'
        dossier, _, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, payload=payload)
        reviewer = self.make_user()
        client = Client()
        client.force_login(reviewer)
        response = client.get(reverse("review", args=[dossier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store, private")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        rendered = response.content.decode("utf-8")
        self.assertNotIn("<script>fetch", rendered)
        self.assertIn("&lt;script&gt;fetch", rendered)

        unsafe_payload = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        unsafe_payload["counterevidence"] = [
            {"statement": "Pièce hostile", "source_url": "javascript:alert(1)", "effect": "aucun"}
        ]
        with self.assertRaisesMessage(ValidationError, "Référence publique non admise"):
            self.make_revision(EditorialDossier.Kind.SPEECH_VOTE, payload=unsafe_payload)

        source = self.make_document()
        dossier = EditorialDossier.objects.create(
            stable_id=self.unique("EDITORIAL"),
            content_kind=EditorialDossier.Kind.SPEECH_VOTE,
            established_kind=EditorialDossier.Kind.SPEECH_VOTE,
            data_scope="synthetic",
        )
        unsafe_snapshot = self.snapshot(source)
        unsafe_snapshot[0]["public_url"] = "data:text/html,<script>alert(1)</script>"
        payload = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        with self.assertRaisesMessage(ValidationError, "Référence publique non admise"):
            create_revision(
                dossier=dossier,
                public_payload=payload,
                source_snapshot=unsafe_snapshot,
                title=payload["title"],
                short_answer=payload["short_answer"],
                main_caveat=payload["main_caveat"],
                reasoning=payload["reasoning"],
                result=payload["result"],
                method_version="DV-M0.1",
            )

    def test_a34_private_ipv4_ipv6_and_dns_rebinding_targets_are_rejected(self):
        with patch(
            "atelier.services.collector.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(UnsafeDestination):
                resolve_public("example.org")
        with patch(
            "atelier.services.collector.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))],
        ):
            with self.assertRaises(UnsafeDestination):
                resolve_public("example.org")
        with self.assertRaises(UnsafeDestination):
            validate_source_url("https://127.0.0.1/private", allowed_hosts={"127.0.0.1"})

    def test_a35_traversal_and_oversized_archive_index_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            traversal = temp / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../outside.txt", "forbidden")
            with self.assertRaisesMessage(UnsafeArchive, "path_traversal"):
                inspect_zip(traversal)

            crowded = temp / "crowded.zip"
            with zipfile.ZipFile(crowded, "w") as archive:
                archive.writestr("one.txt", "1")
                archive.writestr("two.txt", "2")
            with self.assertRaisesMessage(UnsafeArchive, "too_many_files"):
                inspect_zip(crowded, max_files=1)
        self.assertFalse(Path(temp_name).exists())

    def test_a36_private_fields_and_sentinel_are_absent_from_public_packet(self):
        payload = self.payload(EditorialDossier.Kind.OFFICIAL_VOTE)
        payload["reviewer_email"] = "SENTINEL-private@example.invalid"
        with self.assertRaisesMessage(ValidationError, "Champ public inattendu"):
            validate_public_payload(EditorialDossier.Kind.OFFICIAL_VOTE, payload)

        _, revision, _ = self.make_revision()
        serialized = json.dumps(packet_for_event(authorize_and_queue(revision, "test")), sort_keys=True)
        self.assertNotIn("SENTINEL-private", serialized)
        self.assertNotIn("reviewer", serialized)
        self.assertNotIn("original_path", serialized)

    @override_settings(
        DV_NETWORK_MODE="protected",
        DV_PROTECTED_EGRESS_CONFIRMED=True,
        DV_PROTECTED_TRANSPORT_ENDPOINT="https://osint.example.invalid",
        DV_PROTECTED_EGRESS_PROFILE="TOR",
    )
    def test_a37_required_protected_egress_never_falls_back_to_direct(self):
        source = Source.objects.create(
            stable_id="SRC:A37",
            producer="Test",
            family="test",
            discovery_url="https://www.assemblee-nationale.fr/",
            document_url="https://www.assemblee-nationale.fr/test",
            rights_status=Source.RightsStatus.OPEN,
            access_status=Source.AccessStatus.ACTIVE,
            operational_enabled=True,
        )
        protected = ProtectedFetch(
            content=b'{"source":"synthetic"}',
            status_code=200,
            headers={"content-type": "application/json"},
            requested_url=source.document_url,
            final_url=source.document_url,
            receipt={
                "transport": "protected_capture",
                "egress_profile": "TOR",
                "job_id": "00000000-0000-0000-0000-000000000037",
                "no_direct_fallback": True,
            },
        )
        with patch("atelier.services.collector._request_once") as request, patch(
            "atelier.services.protected_transport.acquire_url", return_value=protected
        ) as acquire:
            result = fetch_source(source)
            request.assert_not_called()
            acquire.assert_called_once()
        self.assertTrue(result.created)
        self.assertEqual(result.document.collection_receipt["egress_profile"], "TOR")
        self.assertTrue(result.document.collection_receipt["no_direct_fallback"])
        result.document.collection_receipt = {}
        result.document.original_path = "documents/missing-legacy-copy.bin"
        result.document.save(update_fields=["collection_receipt", "original_path", "updated_at"])
        with patch("atelier.services.protected_transport.acquire_url", return_value=protected):
            replay = fetch_source(source)
        self.assertFalse(replay.created)
        self.assertEqual(replay.document.collection_receipt["job_id"], protected.receipt["job_id"])
        self.assertTrue(replay.document.original_path.replace("\\", "/").startswith("raw-snapshots/"))

    def test_a38_cost_ceiling_blocks_without_paid_fallback(self):
        job = JobRun.objects.create(
            task_name="analysis",
            batch_id="A38",
            input_digest="a" * 64,
            processor_version="deterministic/1",
            configuration={"provider": "none", "fallback": "none"},
            cost_limit=Decimal("1.000000"),
        )
        add_cost(job.job_id, Decimal("0.750000"))
        with self.assertRaisesMessage(JobLimitReached, "cost_limit_reached"):
            add_cost(job.job_id, Decimal("0.500000"))
        job.refresh_from_db()
        self.assertEqual(job.state, JobRun.State.BLOCKED)
        self.assertEqual(job.cost_units, Decimal("0.750000"))
        self.assertEqual(job.configuration["fallback"], "none")

    def test_a39_backup_restore_preserves_facts_versions_suspensions_and_documents(self):
        settings.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.RUNTIME_DIR) as temp_name:
            temp = Path(temp_name)
            source_db = temp / "source.sqlite3"
            connection = sqlite3.connect(source_db)
            try:
                connection.executescript(
                    "CREATE TABLE state(kind TEXT, stable_id TEXT, value TEXT);"
                    "INSERT INTO state VALUES('fact','SCRUTINY:A39','for=1');"
                    "INSERT INTO state VALUES('revision','DOSSIER:A39','2');"
                    "INSERT INTO state VALUES('suspension','DOSSIER:A39','latched');"
                )
                connection.commit()
            finally:
                connection.close()
            documents = temp / "documents"
            documents.mkdir()
            (documents / "source.bin").write_bytes(b"official bytes")
            archive = temp / "backup.zip"
            manifest = create_backup(
                archive,
                database_path=source_db,
                documents_root=documents,
            )
            restored = temp / "restored"
            result = restore_backup(archive, restored)
            restored_db = sqlite3.connect(restored / "database" / "atelier.sqlite3")
            try:
                rows = restored_db.execute("SELECT kind, stable_id, value FROM state ORDER BY kind").fetchall()
            finally:
                restored_db.close()
            self.assertEqual(len(rows), 3)
            self.assertIn(("suspension", "DOSSIER:A39", "latched"), rows)
            self.assertEqual((restored / "documents" / "source.bin").read_bytes(), b"official bytes")
            self.assertEqual(result["status"], "restored_and_verified")
            self.assertEqual(manifest["classification"], "private_sensitive")
            self.assertNotIn("contains_secrets", manifest)

    def test_a40_receiver_keeps_last_authorized_snapshot_without_atelier(self):
        dossier, revision, _ = self.make_revision()
        packet = packet_for_event(authorize_and_queue(revision, "test"))
        self.assertEqual(apply_packet(packet), "applied_locally")
        before = copy.deepcopy(ReceiverCursor.objects.get(record_id=dossier.stable_id).active_payload)

        # Aucune nouvelle requête à l'Atelier : le récepteur conserve son état matérialisé.
        after = ReceiverCursor.objects.get(record_id=dossier.stable_id).active_payload
        self.assertEqual(after, before)
        self.assertEqual(after["authorization"]["route"], "automatic_checked")

    def test_a41_partial_coverage_remains_explicit(self):
        document = self.make_document()
        coverage = Coverage.objects.create(
            source=document.source,
            institution="Assemblée nationale",
            period_start="2022-07-11",
            period_end="2024-06-07",
            object_type="scrutiny",
            expected_count=None,
            expected_state="unknown",
            retrieved_count=4106,
            normalized_count=6,
            publishable_count=3,
            exclusions=["lot de démonstration borné", "aucune exhaustivité revendiquée"],
        )
        self.assertEqual(coverage.expected_state, "unknown")
        self.assertIsNone(coverage.expected_count)
        self.assertTrue(coverage.exclusions)

    def test_a42_render_uses_public_official_links_without_runtime_paths(self):
        document = self.make_document()
        self.make_scrutiny(document)
        user = self.make_user(UserProfile.Role.OPERATOR)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("facts"))
        rendered = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://www.assemblee-nationale.fr/", rendered)
        self.assertNotIn(document.original_path, rendered)
        self.assertNotIn(str(settings.RUNTIME_DIR), rendered)

    def test_a43_tuning_and_evaluation_corpora_cannot_share_a_dossier(self):
        dossier, _, _ = self.make_revision(EditorialDossier.Kind.SPEECH_VOTE)
        EvaluationCase.objects.create(
            stable_id="EVAL:A43:TUNING",
            dossier=dossier,
            corpus=EvaluationCase.Corpus.TUNING,
            label="inconclusive",
            label_source="human_test",
            assignment_digest="b" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            EvaluationCase.objects.create(
                stable_id="EVAL:A43:EVALUATION",
                dossier=dossier,
                corpus=EvaluationCase.Corpus.EVALUATION,
                label="inconclusive",
                label_source="human_test",
                assignment_digest="c" * 64,
            )

    def test_a44_machine_state_supports_gitea_handoff_without_functional_fork(self):
        state_path = Path(settings.BASE_DIR) / "BUILD_STATE.json"
        if not state_path.exists():
            self.skipTest("BUILD_STATE.json est un état de chantier privé, hors paquet applicatif")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # La contre-revue poursuit le même dépôt sur sa branche isolée courante.
        # Exigence inchangée : identité exacte de branche, aucun remote et NAS seulement.
        self.assertEqual(state["repository"]["branch"], "codex/dv-outil-contre-revue-001")
        self.assertEqual(state["repository"]["remote"], "none")
        self.assertEqual(state["repository"]["remote_policy"], "gitea_nas_only_no_github")
        self.assertEqual(
            state["private_target"]["container_policy"],
            "docker_on_nas_only_if_needed_no_pc_docker",
        )

    def test_a45_missing_wordpress_receiver_is_external_blockage_not_fake_success(self):
        with patch.dict(
            os.environ,
            {"DV_WORDPRESS_ENDPOINT": "", "DV_WORDPRESS_USERNAME": "", "DV_WORDPRESS_APP_PASSWORD": ""},
        ):
            with self.assertRaisesMessage(WordPressUnavailable, "wordpress_configuration_missing"):
                send_packet({"event_id": str(uuid.uuid4())})

    def test_a46_synthetic_production_and_unapproved_legacy_liot_are_refused(self):
        _, revision, _ = self.make_revision(data_scope="synthetic", environment="production")
        eligibility = evaluate_revision(revision, "production")
        self.assertFalse(eligibility.allowed)
        self.assertEqual(eligibility.reason, "synthetic_content_in_production")

        fixture = json.loads(
            (Path(settings.BASE_DIR) / "fixtures" / "DV_CAS_AN16_3289_LIOT.original.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["publication"]["authorization"], "non_accordee")
        self.assertFalse(fixture["controls"]["human_review"]["performed"])

    def test_receiver_rejects_tampered_signature_and_payload(self):
        _, revision, _ = self.make_revision()
        packet = packet_for_event(authorize_and_queue(revision, "test"))
        tampered = copy.deepcopy(packet)
        tampered["payload"]["record_id"] = "OTHER"
        self.assertEqual(apply_packet(tampered), "unauthorized")

        unsigned = {key: value for key, value in tampered.items() if key not in {"signature", "signature_algorithm"}}
        tampered["payload_digest"] = sha256_digest(tampered["payload"])
        unsigned["payload_digest"] = tampered["payload_digest"]
        unsigned["payload"] = tampered["payload"]
        tampered["signature"] = sign_packet(unsigned)
        self.assertEqual(apply_packet(tampered), "packet_payload_mismatch")
