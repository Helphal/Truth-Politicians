"""The SQLite backup command must never interpret a SQL schema as a local file."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
from django.test import SimpleTestCase, override_settings
from atelier.services.backup import BackupError, create_backup


class SQLBackupBoundaryTests(SimpleTestCase):
    def test_implicit_sql_name_is_refused_even_when_a_matching_local_file_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SYNTH_schema'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE synth (value TEXT)')
            target = Path(folder) / 'backup.zip'
            with override_settings(DATABASES={'default': {
                    'ENGINE': 'django.db.backends.mysql', 'NAME': str(path)}}):
                with self.assertRaisesMessage(BackupError, 'mariadb_requires_explicit_logical_backup'):
                    create_backup(target, include_documents=False)
            self.assertFalse(target.exists())

    def test_explicit_received_sqlite_snapshot_remains_available(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SYNTH_snapshot.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE synth (value TEXT)')
                db.execute('INSERT INTO synth VALUES (?)', ['SYNTH'])
                db.commit()
            with override_settings(RUNTIME_DIR=Path(folder), DATABASES={'default': {
                    'ENGINE': 'django.db.backends.mysql', 'NAME': 'SYNTH_schema'}}):
                receipt = create_backup(Path(folder) / 'backup.zip', include_documents=False,
                                        database_path=path)
            self.assertEqual(receipt['database_engine'], 'sqlite')
            self.assertEqual(receipt['classification'], 'private_sensitive')
