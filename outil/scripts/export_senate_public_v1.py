"""Projection factuelle ODSEN/DOSLEG. Le dump SQL est lu comme données, jamais exécuté."""
import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from export_public_v1 import Exporter, archive_items, canonical, day, scalar


COPY = re.compile(r'^COPY (\w+) \(([^)]+)\) FROM stdin;\s*$')
POSITIONS = {'1': 'for', '2': 'against', '3': 'abstention', '4': 'non_voting'}
START = '2022-01-01'


def pg_value(value):
    if value == r'\N':
        return None
    def decode(match):
        token = match.group(1)
        if token[0].isdigit():
            return chr(int(token, 8))
        return {'t': '\t', 'n': '\n', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\'}.get(token, token)
    return re.sub(r'\\([0-7]{1,3}|.)', decode, value)


def copy_rows(path, allowed):
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != ['dosleg.sql'] or archive.getinfo('dosleg.sql').file_size > 500_000_000:
            raise ValueError('dosleg_archive_shape')
        with io.TextIOWrapper(archive.open('dosleg.sql'), encoding='utf-8') as stream:
            table, fields = None, []
            for line in stream:
                if table:
                    if line.rstrip('\r\n') == r'\.':
                        table = None
                    elif table in allowed:
                        values = line.rstrip('\r\n').split('\t')
                        if len(values) != len(fields):
                            raise ValueError('dosleg_column_count')
                        yield table, dict(zip(fields, map(pg_value, values)))
                elif match := COPY.match(line):
                    table, columns = match.groups()
                    fields = columns.split(', ')


def professional_text(value):
    # Le dump historique contient quelques caractères C1 issus de Windows-1252.
    return ''.join(bytes([ord(c)]).decode('cp1252') if ord(c) in {0x91, 0x92, 0x93, 0x94, 0x96, 0x97} else c
                   for c in (value or ''))


class SenateExporter(Exporter):
    def __init__(self, sources, output):
        super().__init__(sources, output)
        self.scope = 'Sénat : mandats et affiliations chevauchant 2022+, scrutins nominatifs depuis 2022-01-01'
        self.people = {}
        self.eligible = set()
        self.scrutins = {}
        self.anomalies = []
        self.identities = {}
        self.identity_proofs = {}

    def person_id(self, identifier):
        return self.identities.get(identifier, 'SENAT:PERSON:' + identifier)

    def add_current_territory(self, mandate_id, person):
        if not person.get('territory'):
            return
        row=self.db.execute("SELECT data FROM records WHERE kind='mandate' AND id=?",(mandate_id,)).fetchone()
        data=json.loads(row[0])
        data.update(territory=person['territory'],territory_attested_on=person['observed'],
                    territory_source_external_id=person['source'])
        self.put('mandate',mandate_id,data)

    def link_an_identities(self, manifest_path):
        """Rapprochement sur URL institutionnelle explicite, jamais sur un nom."""
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        people = set()
        for entry in manifest['batches']:
            if entry['type'] != 'person':
                continue
            path = (manifest_path.parent / entry['file']).resolve()
            if not path.is_relative_to(manifest_path.parent.resolve()):
                raise ValueError('manifest_path_escape')
            binary = path.read_bytes()
            if hashlib.sha256(binary).hexdigest() != entry['sha256']:
                raise ValueError('an_manifest_hash_mismatch')
            people.update(record['record_id'] for record in json.loads(binary)['records'])

        def institutional_urls(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from institutional_urls(item)
            elif isinstance(value, list):
                for item in value:
                    yield from institutional_urls(item)
            elif isinstance(value, str) and re.fullmatch(r'https?://(?:www\.)?senat\.fr/senateur/[^/]+\.html', value):
                yield value

        for name in ['AN-AMO50-16', 'AN-AMO50-17', 'AN-AMO30-17']:
            receipt = json.loads((self.sources / (name + '.receipt.json')).read_text(encoding='utf-8'))
            path = self.sources / (name + '.zip')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt['sha256']:
                    raise ValueError('an_identity_source_hash_mismatch')
            for locator, kind, raw in archive_items(path):
                if kind != 'acteur':
                    continue
                pid = 'DV:PERSON:AN:' + str(scalar(raw['uid']))
                if pid not in people:
                    continue
                for url in institutional_urls(raw):
                    match = re.search(r'([0-9]{5}[a-z0-9])\.html$', url, re.I)
                    if not match:
                        continue
                    identifier = match[1].upper()
                    if identifier in self.identities and self.identities[identifier] != pid:
                        raise ValueError('ambiguous_institutional_identity')
                    self.identities[identifier] = pid
                    self.identity_proofs[identifier] = {'person_external_id': pid, 'public_url': url,
                                                        'source_sha256': receipt['sha256'], 'source_locator': locator}
        (self.output / 'IDENTITY-LINKS.json').write_text(json.dumps(self.identity_proofs, indent=2), encoding='utf-8')

    def official_source(self, name):
        path = self.sources / name
        receipt = json.loads(path.with_suffix(path.suffix + '.receipt.json').read_text(encoding='utf-8'))
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if receipt.get('status') != 'downloaded' or actual != receipt.get('sha256'):
            raise ValueError('source_hash_mismatch')
        sid = 'DV:SOURCE:' + name + ':' + actual[:16]
        self.put('source', sid, {'source_type': 'official', 'title': name, 'public_url': receipt['url'],
                                'byte_sha256': actual, 'exact_version': actual,
                                'checked_at': receipt['fetched_at'], 'availability': 'available'})
        self.receipts.append(receipt)
        return path, sid, receipt['fetched_at'][:10]

    def csv_rows(self, name):
        path, sid, observed = self.official_source(name)
        text = path.read_bytes().decode('latin1')
        rows = csv.DictReader(line for line in text.splitlines() if line and not line.startswith('%'))
        return rows, sid, observed

    def actors(self):
        rows, sid, observed = self.csv_rows('SENAT-ODSEN_GENERAL.csv')
        for raw in rows:
            identifier = raw['Matricule']
            # Projection positive immédiate : ni naissance, décès, email, profession privée.
            self.people[identifier] = {'name': raw['Prénom usuel'] + ' ' + raw['Nom usuel'],
                                       'sort': raw['Nom usuel'] + ' ' + raw['Prénom usuel'],
                                       'territory': raw.get('Circonscription','').strip(),
                                       'active': raw['État'] == 'ACTIF', 'source': sid, 'observed': observed}
        self.put('organization', 'SENAT:INSTITUTION', {'type': 'institution', 'raw_type_code':'ODSEN_INSTITUTION', 'slug': 'senat',
                 'name': 'Sénat', 'short_name': 'Sénat', 'chamber': 'Sénat',
                 'official_url': 'https://www.senat.fr/', 'source_external_id': sid})
        rows, sid, observed = self.csv_rows('SENAT-ODSEN_ELUSEN.csv')
        active_documented = set()
        for raw in rows:
            start, end = day(raw['Date de début de mandat']), day(raw['Date de fin de mandat'])
            if not start or (end and end < START) or start > observed:
                self.losses['mandate_outside_period_or_missing_start'] += 1
                continue
            identifier = raw['Matricule']
            if identifier not in self.people:
                self.anomalies.append({'type': 'mandate_actor_missing', 'mandate': raw['Identifiant mandat'], 'actor': identifier})
                continue
            self.eligible.add(identifier)
            # Les dates de mandat sont des jours inclusifs, comme dans le
            # certificat d'annuaire. Une fin renseignée aujourd'hui n'est pas
            # un mandat déjà terminé et ne justifie pas une seconde attestation
            # sans début. Le filtre ci-dessus exclut les débuts futurs.
            current = self.people[identifier]['active'] and (not end or observed <= end)
            if current:
                active_documented.add(identifier)
            self.put('mandate', 'SENAT:MANDATE:' + raw['Identifiant mandat'], {
                'person_external_id': self.person_id(identifier), 'institution_external_id': 'SENAT:INSTITUTION',
                'title': 'Mandat sénatorial', 'chamber': 'Sénat', 'starts_on': start, 'ends_on': end,
                'current_attested_on': observed if current else None,
                'source_external_id': sid, 'source_locator': 'mandate=' + raw['Identifiant mandat']})
            if current:
                self.add_current_territory('SENAT:MANDATE:' + raw['Identifiant mandat'],self.people[identifier])
        # ODSEN_GENERAL atteste ACTIF même lorsque ELUSEN n'expose pas le mandat
        # courant. Ne pas reconstruire sa date de début ni sa circonscription passée.
        for identifier, person in self.people.items():
            if person['active'] and identifier not in active_documented:
                self.eligible.add(identifier)
                self.put('mandate', 'SENAT:CURRENT-ATTESTATION:' + identifier, {
                    'person_external_id': self.person_id(identifier),
                    'institution_external_id': 'SENAT:INSTITUTION', 'title': 'Mandat sénatorial en cours attesté',
                    'chamber': 'Sénat', 'starts_on': None, 'ends_on': None,
                    'current_attested_on': person['observed'], 'source_external_id': person['source'],
                    'source_locator': 'Matricule=' + identifier + ';État=ACTIF'})
                self.add_current_territory('SENAT:CURRENT-ATTESTATION:' + identifier,person)
        self.losses['active_mandate_start_not_documented'] = sum(
            person['active'] and identifier not in active_documented for identifier, person in self.people.items())
        rows, sid, observed = self.csv_rows('SENAT-ODSEN_HISTOGROUPES.csv')
        for raw in rows:
            start, end = day(raw["Date de début d'appartenance"]), day(raw["Date de fin d'appartenance"])
            if raw['Matricule'] not in self.eligible or (end and end < START):
                continue
            group = 'SENAT:GROUP:' + raw['Code du groupe politique']
            self.put('organization', group, {'type': 'group', 'raw_type_code':'ODSEN_GROUP', 'raw_group_code':raw['Code du groupe politique'], 'slug': 'senat-groupe-' + raw['Code du groupe politique'].lower(),
                     'name': raw['Nom court du groupe politique'], 'chamber': 'Sénat', 'source_external_id': sid})
            self.put('affiliation', 'SENAT:AFFILIATION:' + raw['Id appartenance'] + ':' + raw['Id fonction occupée'], {
                'person_external_id': self.person_id(raw['Matricule']), 'organization_external_id': group,
                'starts_on': start, 'ends_on': end, 'role': raw['Nom court fonction'],
                'raw_type_code':'ODSEN_GROUP_MEMBERSHIP',
                'role_starts_on': day(raw['Date de début de la fonction']), 'role_ends_on': day(raw['Date de fin de la fonction']),
                'source_external_id': sid})

    def votes(self):
        path, sid, observed = self.official_source('SENAT-dosleg.zip')
        for table, raw in copy_rows(path, {'scr', 'votsen'}):
            if table == 'scr':
                when = day(raw['scrdat'])
                if not when or not START <= when <= observed:
                    continue
                key = (raw['sesann'], raw['scrnum'])
                identifier = 'SENAT:SCRUTIN:' + ':'.join(key)
                self.scrutins[key] = (identifier, when)
                self.put('scrutin', identifier, {'ballot_number': raw['scrnum'], 'official_title': professional_text(raw['scrint']),
                    'institution_external_id': 'SENAT:INSTITUTION', 'occurred_on': when, 'date_precision': 'day',
                    'count_for': int(raw['scrpou']) if raw['scrpou'] is not None else None,
                    'count_against': int(raw['scrcon']) if raw['scrcon'] is not None else None,
                    'count_abstention': None, 'count_unknown': None,
                    'official_url': f"https://www.senat.fr/scrutin-public/{raw['sesann']}/scr{raw['sesann']}-{raw['scrnum']}.html",
                    'source_external_id': sid, 'source_locator': 'scr:' + ':'.join(key),
                    'source_totals': {k: raw[k] for k in ['scrvot','scrsuf','scrvotsea','scrsufsea','scrpousea','scrconsea']}})
            else:
                key = (raw['sesann'], raw['scrnum'])
                if key not in self.scrutins:
                    continue
                scrutiny, when = self.scrutins[key]
                if raw['senmat'] not in self.people:
                    self.anomalies.append({'type': 'vote_actor_missing', 'scrutin': scrutiny, 'actor': raw['senmat']})
                    continue
                if raw['posvotcod'] not in POSITIONS:
                    raise ValueError('unknown_official_vote_category')
                self.eligible.add(raw['senmat'])
                self.put('vote', scrutiny + ':' + raw['senmat'], {
                    'scrutin_external_id': scrutiny, 'person_external_id': self.person_id(raw['senmat']),
                    'position': POSITIONS[raw['posvotcod']], 'recorded_on': when,
                    'source_category': raw['posvotcod'], 'source_status': raw['stavotidt'],
                    'group_external_id_at_vote': None, 'source_external_id': sid,
                    'source_locator': 'votsen:' + ':'.join(key) + ':' + raw['senmat']})
        self.finish_people()
        (self.output / 'ANOMALIES.json').write_text(json.dumps(self.anomalies, ensure_ascii=False, indent=2), encoding='utf-8')
        self.losses['unresolved_public_references'] = len(self.anomalies)

    def finish_people(self):
        for identifier in self.eligible:
            if identifier in self.identities:
                continue  # Identité déjà livrée par le manifeste AN épinglé.
            p = self.people[identifier]
            self.put('person', 'SENAT:PERSON:' + identifier, {'slug': 'senat-' + identifier.lower(),
                     'display_name': p['name'], 'sort_name': p['sort'], 'notice_status': 'missing',
                     'source_external_id': p['source']})
        self.db.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--an-manifest', type=Path)
    parser.add_argument('--actors-only', action='store_true')
    args = parser.parse_args()
    export = SenateExporter(args.sources.resolve(), args.output.resolve())
    if args.an_manifest:
        export.link_an_identities(args.an_manifest.resolve())
    export.actors()
    if args.actors_only:
        export.scope='Sénat : projection des acteurs, mandats, affiliations et territoires actuels attestés ; aucun snapshot de votes'
        export.finish_people()
    else:
        export.votes()
    export.finish()


if __name__ == '__main__':
    main()
