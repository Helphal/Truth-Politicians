"""Certificats documentaires : population institutionnelle complète, jamais max(date).

Lit les archives officielles gelées et les projections publiques en lecture seule.
La réception Site doit encore rapprocher tous les membres du corpus visible.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sqlite3

from export_public_v1 import archive_items, scalar


def source(root, name, zipped=False):
    path = root / (name + '.zip' if zipped else name)
    receipt_path = root / (name + '.receipt.json')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if receipt.get('status') != 'downloaded' or receipt.get('sha256') != digest:
        raise ValueError('source_non_verifiee')
    return path, receipt, {'id': 'DV:SOURCE:' + name + ':' + digest[:16], 'sha256': digest}


def projection(path, observed):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        members = []
        for rid, encoded in db.execute("SELECT id,data FROM records WHERE kind='mandate' ORDER BY id"):
            data = json.loads(encoded)
            start, end, attested = data.get('starts_on'), data.get('ends_on'), data.get('current_attested_on')
            if ((start and start <= observed and (end and end >= observed or not end and attested and attested >= observed))
                    or not start and attested == observed):
                members.append({'mandate': rid, 'person': data['person_external_id']})
    return members


def qualify(sources, an_projection, senate_projection, identities):
    an_path, an_receipt, an_source = source(sources, 'AN-AMO10-17', True)
    an_people = {'DV:PERSON:AN:' + scalar(raw['uid']) for _, kind, raw in archive_items(an_path) if kind == 'acteur'}
    senate_path, senate_receipt, senate_source = source(sources, 'SENAT-ODSEN_GENERAL.csv')
    _, mandate_receipt, mandate_source = source(sources, 'SENAT-ODSEN_ELUSEN.csv')
    if senate_receipt['fetched_at'][:10] != mandate_receipt['fetched_at'][:10]:
        raise ValueError('sources_senat_non_synchrones')
    links = json.loads(identities.read_text(encoding='utf-8'))
    rows = csv.DictReader(line for line in senate_path.read_bytes().decode('latin1').splitlines() if line and not line.startswith('%'))
    senate_people = {links.get(r['Matricule'], {}).get('person_external_id', 'SENAT:PERSON:' + r['Matricule'])
                     for r in rows if r['État'] == 'ACTIF'}
    results = []
    for chamber, receipt, required, projected, refs in (
        ('assemblee', an_receipt, an_people, an_projection, [an_source]),
        ('senat', senate_receipt, senate_people, senate_projection, [senate_source, mandate_source]),
    ):
        day = receipt['fetched_at'][:10]
        members = projection(projected, day)
        if not required or {m['person'] for m in members} != required:
            raise ValueError('population_complete_non_rapprochee:' + chamber)
        results.append({'schema': 'dv-directory-snapshot/1', 'chamber': chamber, 'as_of': day,
                        'basis': 'population_complete_source_institutionnelle', 'sources': refs,
                        'members': members, 'person_count': len(required)})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for arg in ('sources', 'an-projection', 'senate-projection', 'identities', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    args = parser.parse_args()
    results = qualify(args.sources, args.an_projection, args.senate_projection, args.identities)
    args.output.mkdir(parents=True, exist_ok=False)
    for result in results:
        (args.output / (result['chamber'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{'chamber': r['chamber'], 'as_of': r['as_of'], 'people': r['person_count'], 'mandates': len(r['members'])} for r in results]))
