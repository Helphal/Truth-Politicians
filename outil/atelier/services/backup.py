"""Sauvegarde SQLite + originaux et restauration vérifiable en zone isolée."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import connection

from .canonical import canonical_bytes


class BackupError(RuntimeError):
    pass


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_backup(
    destination: Path,
    *,
    include_documents: bool = True,
    database_path: Path | None = None,
    documents_root: Path | None = None,
) -> dict:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    explicit_database = database_path is not None
    if not explicit_database and settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3':
        raise BackupError('mariadb_requires_explicit_logical_backup')
    database_path = (database_path or Path(settings.DATABASES["default"]["NAME"])).resolve()
    if not database_path.exists():
        raise BackupError("database_missing")
    settings.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.RUNTIME_DIR) as temp_name:
        temp = Path(temp_name)
        database_copy = temp / "atelier.sqlite3"
        if not explicit_database:
            connection.close()
        source_db = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        target_db = sqlite3.connect(database_copy)
        try:
            source_db.backup(target_db)
            if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("database_integrity_failure")
        finally:
            target_db.close()
            source_db.close()

        files = [("database/atelier.sqlite3", database_copy)]
        if include_documents:
            if documents_root is not None:
                document_roots = [("", documents_root.resolve())]
            else:
                document_roots = [
                    ("raw-snapshots", (settings.RUNTIME_DIR / "raw-snapshots").resolve()),
                    ("evidence-vault", Path(settings.DV_EVIDENCE_VAULT_PATH).resolve()),
                ]
            seen_roots: set[Path] = set()
            for label, raw_root in document_roots:
                if raw_root in seen_roots:
                    continue
                seen_roots.add(raw_root)
                if raw_root.exists():
                    for path in sorted(raw_root.rglob("*")):
                        if path.is_file():
                            relative = path.relative_to(raw_root).as_posix()
                            archive_name = f"documents/{label}/{relative}" if label else f"documents/{relative}"
                            files.append((archive_name, path))
        manifest = {
            "format": "dv-backup/3",
            "database_engine": "sqlite",
            "classification": "private_sensitive",
            "files": [{"path": name, "sha256": _hash(path), "bytes": path.stat().st_size} for name, path in files],
        }
        temp_archive = temp / "backup.part"
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", canonical_bytes(manifest))
            for name, path in files:
                archive.write(path, name)
        os.replace(temp_archive, destination)
    return {**manifest, "archive_sha256": _hash(destination), "archive_bytes": destination.stat().st_size}


def restore_backup(archive_path: Path, destination: Path) -> dict:
    archive_path = archive_path.resolve()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("restore_destination_not_empty")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise BackupError("archive_path_traversal")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise BackupError("manifest_invalid") from exc
        if manifest.get("format") not in {"dv-backup/2", "dv-backup/3"} or manifest.get("classification") != "private_sensitive":
            raise BackupError("backup_format_unknown")
        for item in manifest.get("files", []):
            if set(item) != {"path", "sha256", "bytes"} or item["path"] not in names:
                raise BackupError("manifest_entry_invalid")
            target = (destination / PurePosixPath(item["path"])).resolve()
            if destination != target and destination not in target.parents:
                raise BackupError("restore_path_escape")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(item["path"]) as source, target.open("xb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != item["sha256"] or target.stat().st_size != item["bytes"]:
                raise BackupError("restored_file_mismatch")
    restored_db = destination / "database" / "atelier.sqlite3"
    restored_connection = sqlite3.connect(f"file:{restored_db.as_posix()}?mode=ro", uri=True)
    try:
        if restored_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("restored_database_integrity_failure")
        table_count = restored_connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        restored_connection.close()
    # Une restauration ne rétablit jamais implicitement l'autorité d'envoi.
    (destination / ".dv-restored-unreconciled").write_text("receiver_reconciliation_required\n", encoding="utf-8")
    return {"status": "restored_and_verified", "files": len(manifest["files"]), "tables": table_count}
