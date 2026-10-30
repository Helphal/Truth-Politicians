from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from atelier.services.backup import create_backup
from atelier.services.evidence_vault import EvidenceVaultError, plan_capture, stage_public_record, store_private_capture


class EvidenceVaultTests(SimpleTestCase):
    def _settings(self, root: Path, *, quota: int = 1024 * 1024):
        runtime = root / "runtime"
        return override_settings(
            RUNTIME_DIR=runtime,
            STATIC_ROOT=runtime / "static",
            DV_EVIDENCE_VAULT_PATH=runtime / "evidence-vault",
            DV_PUBLIC_EVIDENCE_STAGING_PATH=runtime / "public-evidence-staging",
            DV_EVIDENCE_VAULT_MAX_BYTES=quota,
        )

    def test_private_capture_is_deduplicated_and_stays_outside_static(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self._settings(root):
                first = store_private_capture(
                    b"public statement",
                    source_url="https://example.org/interview?utm_source=test",
                    source_kind="press",
                    media_type="text/html",
                    captured_at=datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
                    suffix=".html",
                )
                second = store_private_capture(
                    b"public statement",
                    source_url="https://example.org/interview",
                    source_kind="press",
                    media_type="text/html",
                    captured_at=datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
                    suffix=".html",
                )
                self.assertTrue(first["created"])
                self.assertFalse(second["created"])
                self.assertNotIn("utm_source", first["source_url"])
                self.assertFalse((root / "runtime" / "static" / first["object_path"]).exists())

    def test_public_record_contains_no_private_path_or_network_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self._settings(root):
                private = store_private_capture(
                    b"statement",
                    source_url="https://media.example/video/42",
                    source_kind="video",
                    media_type="video/mp4",
                    captured_at=datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
                )
                private["collection_receipt"] = {"source_ip": "192.0.2.4"}
                public = stage_public_record(
                    private,
                    attribution="Chaîne publique",
                    excerpt="Passage vérifié.",
                    archive_url="https://web.archive.org/web/20260914/https://media.example/video/42",
                )
                stored = json.loads((root / "runtime" / "public-evidence-staging" / public["staged_path"]).read_text())
                serialized = json.dumps(stored)
                self.assertNotIn("object_path", serialized)
                self.assertNotIn("collection_receipt", serialized)
                self.assertNotIn("192.0.2.4", serialized)
                self.assertEqual(stored["capture"]["sha256"], private["sha256"])

    def test_private_or_sensitive_public_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self._settings(root):
                with self.assertRaisesMessage(EvidenceVaultError, "public_url_private_address"):
                    store_private_capture(
                        b"x",
                        source_url="https://10.0.0.1/private",
                        source_kind="social",
                        media_type="text/plain",
                        captured_at=datetime.now(timezone.utc),
                    )
                with self.assertRaisesMessage(EvidenceVaultError, "public_url_sensitive_query"):
                    store_private_capture(
                        b"x",
                        source_url="https://example.org/watch?access_token=value",
                        source_kind="video",
                        media_type="text/plain",
                        captured_at=datetime.now(timezone.utc),
                    )

    def test_quota_fails_closed_but_existing_object_remains_reusable(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self._settings(root, quota=4):
                first = store_private_capture(
                    b"1234",
                    source_url="https://example.org/a",
                    source_kind="press",
                    media_type="text/plain",
                    captured_at=datetime.now(timezone.utc),
                )
                self.assertFalse(store_private_capture(
                    b"1234",
                    source_url="https://example.org/a",
                    source_kind="press",
                    media_type="text/plain",
                    captured_at=datetime.now(timezone.utc),
                )["created"])
                with self.assertRaisesMessage(EvidenceVaultError, "vault_quota_exceeded"):
                    store_private_capture(
                        b"5",
                        source_url="https://example.org/b",
                        source_kind="press",
                        media_type="text/plain",
                        captured_at=datetime.now(timezone.utc),
                    )
                self.assertTrue(first["sha256"])

    def test_default_backup_includes_legacy_originals_and_private_vault(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            legacy = runtime / "raw-snapshots"
            vault = runtime / "evidence-vault"
            legacy.mkdir(parents=True)
            vault.mkdir(parents=True)
            (legacy / "legacy.bin").write_bytes(b"legacy")
            (vault / "vault.bin").write_bytes(b"vault")
            database = root / "source.sqlite3"
            import sqlite3
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE proof(value TEXT)")
            connection.commit()
            connection.close()
            archive = runtime / "backups" / "backup.zip"
            with self._settings(root):
                manifest = create_backup(archive, database_path=database)
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
            self.assertEqual(manifest["format"], "dv-backup/3")
            self.assertIn("documents/raw-snapshots/legacy.bin", names)
            self.assertIn("documents/evidence-vault/vault.bin", names)

    def test_tiered_policy_keeps_full_video_exceptional_and_wayback_closed(self):
        default_video = plan_capture(source_kind="video")
        self.assertEqual(default_video["storage_tier"], "light")
        self.assertNotIn("original_media", default_video["retained"])
        self.assertIn("transcript", default_video["retained"])
        exceptional_video = plan_capture(source_kind="video", full_media_exception=True)
        self.assertEqual(exceptional_video["storage_tier"], "full_media")
        self.assertIn("original_media", exceptional_video["retained"])
        fragile_page = plan_capture(source_kind="press", fragile=True)
        self.assertEqual(fragile_page["storage_tier"], "web_archive")
        self.assertFalse(fragile_page["wayback_submission_authorized"])
        self.assertFalse(fragile_page["public_export_authorized"])

    def test_storage_tier_is_recorded_and_invalid_tier_fails_closed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self._settings(root):
                record = store_private_capture(
                    b"page",
                    source_url="https://example.org/page",
                    source_kind="press",
                    media_type="text/html",
                    captured_at=datetime.now(timezone.utc),
                    storage_tier="web_archive",
                )
                self.assertEqual(record["storage_tier"], "web_archive")
                with self.assertRaisesMessage(EvidenceVaultError, "storage_tier_invalid"):
                    store_private_capture(
                        b"page",
                        source_url="https://example.org/page",
                        source_kind="press",
                        media_type="text/html",
                        captured_at=datetime.now(timezone.utc),
                        storage_tier="unbounded",
                    )
