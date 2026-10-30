"""Private logical Atelier snapshot; restore only into a fresh isolated schema.

No schema/account is created here. No authority or service is reactivated.
Operators must reconcile current receipts/rights/withdrawals before any use.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile

from .sql_transport import cli_options, mysql_options, verify_session


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', value):
        raise ValueError('invalid_schema_or_table_identifier')
    return '`' + value + '`'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def connect(config, database=None):
    import MySQLdb
    db = MySQLdb.connect(host=config['HOST'], port=int(config['PORT']),
        user=config['USER'], passwd=config['PASSWORD'], db=database or config['NAME'],
        charset='utf8mb4', **mysql_options(config))
    verify_session(db, config)
    return db


def client(config, binary, database, *, source=None, output=None, dump=False):
    identifier(database)
    command = [str(binary), '--no-defaults', '--protocol=tcp', '--host=' + config['HOST'],
        '--port=' + str(config['PORT']), '--user=' + config['USER'], '--default-character-set=utf8mb4',
        *cli_options(config)]
    if dump:
        command += ['--single-transaction', '--quick', '--skip-lock-tables',
            '--skip-add-locks', '--skip-add-drop-table', '--skip-comments', '--hex-blob']
    command.append(database)
    result = subprocess.run(command, env=dict(os.environ, MYSQL_PWD=config['PASSWORD']),
        stdin=source if source is not None else subprocess.DEVNULL, stdout=output or subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('atelier_mariadb_dump_failed' if dump else 'atelier_mariadb_load_failed')


def create_backup(config, bin_dir, archive, *, application_version, documents_root=None):
    identifier(config['NAME'])
    archive, bin_dir = Path(archive).resolve(), Path(bin_dir).resolve(strict=True)
    if archive.exists() or archive.with_suffix(archive.suffix + '.sha256').exists():
        raise ValueError('backup_destination_exists')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(config)) as db, db.cursor() as cursor:
        transport = verify_session(db, config)
        cursor.execute('SELECT VERSION()')
        server_version = cursor.fetchone()[0]
        cursor.execute('SELECT app,name FROM django_migrations ORDER BY app,name')
        migrations = cursor.fetchall()
        cursor.execute('SHOW TRIGGERS')
        if cursor.fetchone():
            raise ValueError('unsupported_trigger_schema')
        cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND ENGINE<>'InnoDB'", (config['NAME'],))
        if cursor.fetchone():
            raise ValueError('snapshot_requires_innodb_only')
    with tempfile.TemporaryDirectory(prefix='dv-atelier-mariadb-', dir=archive.parent) as folder:
        sql = Path(folder) / 'database.sql'
        with sql.open('xb') as output:
            client(config, bin_dir / ('mariadb-dump.exe' if os.name == 'nt' else 'mariadb-dump'),
                   config['NAME'], output=output, dump=True)
        files = [('database.sql', sql)]
        if documents_root is not None:
            vault = Path(documents_root).resolve(strict=True)
            for path in sorted(vault.rglob('*')):
                if path.is_file():
                    if not path.resolve().is_relative_to(vault):
                        raise ValueError('evidence_path_escape')
                    files.append(('documents/' + path.relative_to(vault).as_posix(), path))
        manifest = {'schema': 'dv.atelier-mariadb-backup/1', 'engine': 'mariadb',
            'classification': 'private_sensitive', 'method': 'single_transaction_logical_dump',
            'status': 'complete', 'application_version': application_version,
            'created_at': datetime.now(timezone.utc).isoformat(), 'server_version': server_version,
            'migrations': migrations, 'sql_transport': transport,
            'files': [{'path': name, 'sha256': sha256(path), 'bytes': path.stat().st_size}
                      for name, path in files]}
        with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
            bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            for name, path in files:
                bundle.write(path, name)
    archive.chmod(0o600)
    digest = sha256(archive)
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '\n', encoding='ascii')
    return {**manifest, 'archive_sha256': digest}


def restore_backup(config, bin_dir, archive, expected, *, database, destination):
    identifier(database)
    if database == config['NAME']:
        raise ValueError('restore_cannot_target_current_database')
    if not re.fullmatch('[0-9a-f]{64}', expected or '') or sha256(archive) != expected:
        raise ValueError('archive_digest_mismatch')
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('restore_destination_exists')
    with closing(connect(config, database)) as db, db.cursor() as cursor:
        cursor.execute('SHOW TABLES')
        if cursor.fetchone():
            raise ValueError('restore_schema_must_be_empty')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise ValueError('unexpected_archive_members')
        if bundle.getinfo('manifest.json').file_size > 1024 * 1024:
            raise ValueError('invalid_backup_manifest')
        manifest = json.loads(bundle.read('manifest.json'))
        if (manifest.get('schema') != 'dv.atelier-mariadb-backup/1'
                or manifest.get('classification') != 'private_sensitive'
                or manifest.get('status') != 'complete' or not isinstance(manifest.get('files'), list)):
            raise ValueError('invalid_backup_manifest')
        listed = [row.get('path') for row in manifest['files']]
        if set(names) != {'manifest.json', *listed} or len(listed) != len(set(listed)) or 'database.sql' not in listed:
            raise ValueError('unexpected_archive_members')
        for row in manifest['files']:
            if set(row) != {'path', 'bytes', 'sha256'}:
                raise ValueError('invalid_backup_manifest')
            relative = PurePosixPath(row['path'])
            if (relative.is_absolute() or '..' in relative.parts or '\\' in row['path'] or ':' in row['path']
                    or (row['path'] != 'database.sql' and not row['path'].startswith('documents/'))):
                raise ValueError('archive_path_traversal')
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise ValueError('restore_path_escape')
            info = bundle.getinfo(row['path'])
            if (type(row['bytes']) is not int or row['bytes'] < 0 or info.file_size != row['bytes']
                    or info.file_size > 100 * 1024**3):
                raise ValueError('invalid_backup_manifest')
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open('xb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(block)
            if sha256(target) != row['sha256']:
                raise ValueError('restored_file_mismatch')
    with (destination / 'database.sql').open('rb') as source:
        client(config, Path(bin_dir) / ('mariadb.exe' if os.name == 'nt' else 'mariadb'), database, source=source)
    with closing(connect(config, database)) as db, db.cursor() as cursor:
        cursor.execute('SELECT app,name FROM django_migrations ORDER BY app,name')
        if list(map(list, cursor.fetchall())) != manifest['migrations']:
            raise ValueError('restored_migrations_mismatch')
        cursor.execute('SHOW TABLES')
        tables = cursor.fetchall()
        for (table,) in tables:
            cursor.execute('CHECK TABLE ' + identifier(table))
            if any(row[2] == 'error' or (row[2] == 'status' and row[3] != 'OK') for row in cursor.fetchall()):
                raise ValueError('restored_table_integrity_failure')
        # Restored credentials remain documentary state; an old browser session
        # must never become a fresh authenticated session after rollback.
        cursor.execute('DELETE FROM django_session')
        sessions_invalidated = cursor.rowcount
        db.commit()
    (destination / '.dv-restored-unreconciled').write_text('current_receipts_rights_and_authority_required\n', encoding='ascii')
    return {'schema': 'dv.atelier-mariadb-restore/1', 'status': 'restored_and_verified_isolated_only',
            'archive_sha256': expected, 'tables': len(tables), 'migrations': manifest['migrations'],
            'sessions_invalidated': sessions_invalidated,
            'authority_reactivated': False, 'services_started': False, 'current_database_written': False}
