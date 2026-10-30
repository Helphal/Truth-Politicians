"""Digests always identify closed files, never a newline-normalized string."""
import hashlib
import json
from pathlib import Path
import zipfile


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_evidence(path, text):
    path = Path(path)
    # Exclusive creation also protects historical evidence against reruns.
    with path.open('xb') as stream:
        stream.write(text.encode('utf-8'))
    return digest_file(path)


def verify_logs(folder):
    folder = Path(folder)
    manifest = json.loads((folder / 'resultats.json').read_bytes())
    results = []
    for entry in manifest['steps']:
        name = entry['step'] + '.txt'
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('unsafe_log_name')
        data = (folder / name).read_bytes()
        raw = hashlib.sha256(data).hexdigest()
        results.append({'file': name, 'expected': entry['log_sha256'], 'raw_sha256': raw,
                        'passed': raw == entry['log_sha256']})
    return {'passed': bool(results) and all(x['passed'] for x in results), 'logs': results}


def verify_archive_logs(archive_path, prefix=''):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(prefix + 'resultats.json'))
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate_archive_member')
        results = []
        for entry in manifest['steps']:
            name = entry['step'] + '.txt'
            if Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('unsafe_log_name')
            raw = hashlib.sha256(archive.read(prefix + name)).hexdigest()
            results.append({'file': name, 'raw_sha256': raw,
                            'passed': raw == entry['log_sha256']})
        return {'passed': bool(results) and all(x['passed'] for x in results), 'logs': results}
