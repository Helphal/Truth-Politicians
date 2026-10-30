"""Export public AN épinglé, sans accès aux comptes ou aux bases privées.

Lit uniquement les archives officielles désignées par leurs reçus SHA256.
Projection positive : noms professionnels, identifiants, mandats, groupes,
partis explicitement codés et votes nominatifs. Aucune adresse ou naissance.
"""
from collections import Counter
from datetime import date, datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile
from organization_semantics import BRIDGE, classify_organization, classify_relation


SCHEMA = BRIDGE
TYPE_ORDER = ['source', 'organization', 'person', 'mandate', 'affiliation', 'theme', 'dossier',
              'text', 'text_version', 'text_theme', 'scrutin', 'vote', 'coverage']
KINDS = {'ASSEMBLEE': 'institution', 'GP': 'group', 'PARPOL': 'party', 'GROUPESENAT': 'group'}
POSITIONS = {'pours': 'for', 'contres': 'against', 'abstentions': 'abstention',
             'pour': 'for', 'contre': 'against', 'abstention': 'abstention',
             'nonVotants': 'non_voting', 'nonVotantsVolontaires': 'non_voting'}


def scalar(value):
    return value.get('#text', '') if isinstance(value, dict) else (value or '')


def sequence(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def day(value):
    if not value:
        return None
    return date.fromisoformat(str(value)[:10]).isoformat()


def archive_items(path):
    """Pas d'extraction : traversal/symlink ne peut pas écrire sur le disque."""
    with zipfile.ZipFile(path) as archive:
        total = 0
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir() or not info.filename.lower().endswith('.json'):
                continue
            parts = PurePosixPath(info.filename).parts
            if '..' in parts or info.filename.startswith(('/', '\\')) or info.file_size > 16 * 1024 * 1024:
                raise ValueError('unsafe_archive_member')
            total += info.file_size
            if total > 3 * 1024 ** 3:
                raise ValueError('archive_expansion_limit')
            payload = json.loads(archive.read(info).decode('utf-8-sig'))
            if not isinstance(payload, dict):
                raise ValueError('archive_shape_invalid')
            for kind, record in payload.items():
                if kind in {'acteur', 'mandat', 'organe', 'scrutin', 'dossierParlementaire', 'document'}:
                    if not isinstance(record, dict):
                        raise ValueError('record_shape_invalid')
                    yield info.filename, kind, record
                    if kind == 'acteur':
                        for mandate in sequence((record.get('mandats') or {}).get('mandat')):
                            if not isinstance(mandate, dict):
                                raise ValueError('nested_mandate_shape_invalid')
                            child = dict(mandate)
                            child.setdefault('acteurRef', scalar(record.get('uid')))
                            yield info.filename + '#mandat=' + str(scalar(child.get('uid'))), 'mandat', child


class Exporter:
    def __init__(self, sources, output):
        self.sources, self.output = sources, output
        output.mkdir(parents=True, exist_ok=False)
        self.db = sqlite3.connect(output / 'public-projection.sqlite3')
        self.db.executescript('''
          CREATE TABLE records(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id));
          CREATE TABLE actors(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE mandates(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE eligible(id TEXT PRIMARY KEY);
        ''')
        self.losses = Counter()
        self.receipts = []
        self.today = datetime.now(timezone.utc).date().isoformat()
        self.pending = 0
        self.scope = 'Assemblée nationale XVI/XVII ; scrutins du Congrès explicitement séparés ; Sénat ordinaire non inclus'

    def put(self, kind, identifier, data):
        if not identifier:
            raise ValueError('missing_stable_identifier')
        if kind == 'organization' and data.get('raw_type_code'):
            data = classify_organization(data, data['raw_type_code'])
        elif kind == 'affiliation' and data.get('raw_type_code'):
            org = self.db.execute("SELECT data FROM records WHERE kind='organization' AND id=?", (data['organization_external_id'],)).fetchone()
            data = classify_relation(data, json.loads(org[0]) if org else {'type':'unqualified'}, data['raw_type_code'])
        self.db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)', (kind, identifier, canonical(data)))
        self.pending += 1
        if self.pending >= 5000:
            self.db.commit()
            self.pending = 0

    def source(self, name):
        receipt = json.loads((self.sources / (name + '.receipt.json')).read_text(encoding='utf-8'))
        path = self.sources / (name + '.zip')
        with path.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if receipt.get('status') != 'downloaded' or actual != receipt['sha256']:
            raise ValueError('source_hash_mismatch')
        sid = 'DV:SOURCE:' + name + ':' + actual[:16]
        self.put('source', sid, {'source_type': 'official', 'title': name,
                                'public_url': receipt['url'], 'byte_sha256': actual,
                                'exact_version': actual, 'checked_at': receipt['fetched_at'],
                                'availability': 'available'})
        self.receipts.append(receipt)
        return path, sid

    def actors(self, name):
        path, sid = self.source(name)
        observed_on = self.receipts[-1]['fetched_at'][:10]
        for locator, kind, raw in archive_items(path):
            uid = str(scalar(raw.get('uid')))
            provenance = {'source_external_id': sid, 'source_locator': locator}
            if kind == 'acteur':
                ident = (raw.get('etatCivil') or {}).get('ident') or {}
                display = ' '.join(str(scalar(ident.get(k))).strip() for k in ('prenom', 'nom')).strip()
                if not uid or not display:
                    self.losses['actor_without_id_or_name'] += 1
                    continue
                data = {'slug': 'an-' + uid.lower(), 'display_name': display,
                        'sort_name': ' '.join(str(scalar(ident.get(k))).strip() for k in ('nom', 'prenom')).strip(),
                        'aliases': [], **provenance}
                self.db.execute('INSERT OR REPLACE INTO actors VALUES(?,?)', (uid, canonical(data)))
            elif kind == 'organe' and raw.get('codeType') in KINDS:
                life = raw.get('viMoDe') or {}
                label = str(scalar(raw.get('libelle')))
                if not uid or not label:
                    self.losses['organization_without_id_or_name'] += 1
                    continue
                self.put('organization', 'DV:ORG:AN:' + uid,
                         {'slug': 'an-' + uid.lower(), 'name': label,
                          'short_name': str(scalar(raw.get('libelleAbrege'))),
                          'type': KINDS[raw['codeType']],
                          'raw_type_code': raw['codeType'],
                          'chamber': 'Sénat' if raw['codeType'] == 'GROUPESENAT' else 'Assemblée nationale',
                          'valid_from': day(life.get('dateDebut')), 'valid_to': day(life.get('dateFin')),
                          **provenance})
            elif kind == 'mandat' and raw.get('typeOrgane') in KINDS:
                start, end = day(raw.get('dateDebut')), day(raw.get('dateFin'))
                if not start or start > self.today or (end and end < '2022-06-22'):
                    self.losses['mandate_outside_period_or_missing_start'] += 1
                    continue
                if end and end < start:
                    self.losses['mandate_invalid_interval'] += 1
                    continue
                actor = str(scalar(raw.get('acteurRef')))
                refs = [str(scalar(value)) for value in sequence((raw.get('organes') or {}).get('organeRef'))]
                data = {'actor': actor, 'type': raw['typeOrgane'], 'refs': refs,
                        'start': start, 'end': end, 'legislature': str(scalar(raw.get('legislature'))),
                        'role': str(scalar((raw.get('infosQualite') or {}).get('libQualite'))),
                        **provenance}
                place = (raw.get('election') or {}).get('lieu') or {}
                data['territorial_fields'] = {
                    key: str(scalar(place.get(source))) for key, source in
                    [('region', 'region'), ('department', 'departement'),
                     ('department_code', 'numDepartement'), ('constituency', 'numCirco')]}
                if name == 'AN-AMO10-17' and end is None:
                    data['current_attested_on'] = observed_on
                self.db.execute('INSERT OR REPLACE INTO mandates VALUES(?,?)', (uid, canonical(data)))
                if raw['typeOrgane'] == 'ASSEMBLEE' and data['legislature'] in {'16', '17'}:
                    self.db.execute('INSERT OR IGNORE INTO eligible VALUES(?)', (actor,))
        self.db.commit()

    def finalize_actors(self):
        for uid, encoded in self.db.execute('SELECT a.id,a.data FROM actors a JOIN eligible e ON e.id=a.id'):
            self.put('person', 'DV:PERSON:AN:' + uid, json.loads(encoded))
        for uid, encoded in self.db.execute('SELECT id,data FROM mandates ORDER BY id'):
            data = json.loads(encoded)
            pid = 'DV:PERSON:AN:' + data['actor']
            if not self.db.execute('SELECT 1 FROM records WHERE kind=? AND id=?', ('person', pid)).fetchone():
                self.losses['mandate_actor_outside_period_or_unresolved'] += 1
                continue
            if len(data['refs']) != 1:
                self.losses['mandate_organization_ambiguous'] += 1
                continue
            oid = 'DV:ORG:AN:' + data['refs'][0]
            if not self.db.execute('SELECT 1 FROM records WHERE kind=? AND id=?', ('organization', oid)).fetchone():
                self.losses['mandate_organization_unresolved'] += 1
                continue
            common = {'person_external_id': pid, 'starts_on': data['start'], 'ends_on': data['end'],
                      'source_external_id': data['source_external_id'], 'source_locator': data['source_locator']}
            if data['type'] == 'ASSEMBLEE':
                if data['legislature'] not in {'16', '17'}:
                    continue
                self.put('mandate', 'DV:MANDATE:AN:' + uid,
                         {**common, 'institution_external_id': oid, 'chamber': 'Assemblée nationale',
                          'title': data['role'], 'legislature': data['legislature'], **data['territorial_fields']})
                if data.get('current_attested_on'):
                    row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?',
                                          ('mandate', 'DV:MANDATE:AN:' + uid)).fetchone()
                    attested = json.loads(row[0])
                    attested['current_attested_on'] = data['current_attested_on']
                    self.put('mandate', 'DV:MANDATE:AN:' + uid, attested)
            else:
                self.put('affiliation', 'DV:AFF:AN:' + uid,
                         {**common, 'organization_external_id': oid, 'role': data['role'], 'raw_type_code': data['type']})
        self.db.commit()

    def documents(self, name):
        path, sid = self.source(name)
        # Deux passages : les versions ne créent jamais un dossier absent de la source.
        for locator, kind, raw in archive_items(path):
            if kind != 'dossierParlementaire' or str(raw.get('legislature')) not in {'16', '17'}:
                continue
            uid = scalar(raw.get('uid'))
            title = (raw.get('titreDossier') or {}).get('titre')
            chemin = (raw.get('titreDossier') or {}).get('titreChemin')
            if not uid or not title:
                self.losses['dossier_missing_identity_or_title'] += 1
                continue
            self.put('dossier', 'DV:DOSSIER:AN:' + uid, {
                'slug': 'an-' + uid.lower(), 'title': title,
                'official_url': f"https://www.assemblee-nationale.fr/dyn/{raw['legislature']}/dossiers/{chemin}" if chemin else '',
                'source_external_id': sid, 'source_locator': locator,
                'procedure_documented': raw.get('procedureParlementaire')})
        for locator, kind, raw in archive_items(path):
            if kind != 'document' or str(raw.get('legislature')) not in {'16', '17'}:
                continue
            uid = scalar(raw.get('uid'))
            title = (raw.get('titres') or {}).get('titrePrincipal')
            dossier = 'DV:DOSSIER:AN:' + str(raw.get('dossierRef') or '')
            if not uid or not title:
                self.losses['document_missing_identity_or_title'] += 1
                continue
            if not self.db.execute("SELECT 1 FROM records WHERE kind='dossier' AND id=?", (dossier,)).fetchone():
                self.losses['document_dossier_not_resolved'] += 1
                dossier = None
            # Le suffixe AN identifie un document de l'Assemblée ; pas d'URL AN
            # fabriquée pour un document sénatorial référencé dans le dossier.
            url = 'https://www.assemblee-nationale.fr/dyn/opendata/' + uid + '.html' if 'ANR5' in uid else ''
            text = 'DV:TEXT:AN:' + uid
            provenance = {'source_external_id': sid, 'source_locator': locator}
            self.put('text', text, {'slug': 'an-' + uid.lower(), 'official_title': title,
                     'short_title': (raw.get('titres') or {}).get('titrePrincipalCourt') or '',
                     'dossier_external_id': dossier, 'official_url': url,
                     'nature': 'document parlementaire', **provenance})
            chronology = (raw.get('cycleDeVie') or {}).get('chrono') or {}
            self.put('text_version', 'DV:VERSION:AN:' + uid, {
                'text_external_id': text, 'official_title': title, 'version_label': uid,
                'stage': raw.get('provenance') or '', 'occurred_on': day(chronology.get('dateDepot')),
                'official_url': url, 'institution_external_id': None, **provenance})
        self.db.commit()

    def votes(self, name, legislature):
        path, sid = self.source(name)
        people = {row[0] for row in self.db.execute("SELECT id FROM records WHERE kind='person'")}
        orgs = {row[0] for row in self.db.execute("SELECT id FROM records WHERE kind='organization'")}
        count = 0
        for locator, kind, raw in archive_items(path):
            if kind != 'scrutin':
                continue
            uid, number = str(scalar(raw.get('uid'))), str(scalar(raw.get('numero')))
            when = day(raw.get('dateScrutin'))
            title = str(scalar(raw.get('titre')))
            minimum, maximum = ('2022-06-22', '2024-06-09') if legislature == 16 else ('2024-01-01', self.today)
            if not uid or not number or not when or not title or not minimum <= when <= maximum:
                self.losses['scrutin_outside_period_or_missing_required_fact'] += 1
                continue
            scrutiny_id = 'DV:SCRUTIN:AN:' + uid
            counts = (raw.get('syntheseVote') or {}).get('decompte') or {}
            groups = (((raw.get('ventilationVotes') or {}).get('organe') or {}).get('groupes') or {}).get('groupe')
            observations = []
            for group in sequence(groups):
                nominative = ((group.get('vote') or {}).get('decompteNominatif') or {})
                for category, voters in nominative.items():
                    if voters is None:
                        continue
                    if category not in POSITIONS:
                        self.losses['unsupported_nominative_category'] += 1
                        continue
                    if isinstance(voters, dict) and 'votant' in voters:
                        voters = voters['votant']
                    for voter in sequence(voters):
                        if not isinstance(voter, dict) or not voter.get('acteurRef'):
                            self.losses['vote_actor_reference_missing'] += 1
                            continue
                        observations.append((str(voter['acteurRef']), category, str(group.get('organeRef') or '')))
            duplicates = Counter(item[0] for item in observations)
            def integer(key):
                value = counts.get(key)
                return None if value is None or value == '' else int(value)
            institution = 'DV:ORG:AN:' + str(raw.get('organeRef') or '')
            congress = uid.startswith('VTCG') and raw.get('lieuVote') == 'Congrès'
            if congress:
                institution = 'DV:INSTITUTION:CONGRES'
                self.put('organization', institution, {'slug': 'congres', 'name': 'Congrès',
                         'type': 'institution', 'raw_type_code':'AN_CONGRES_LIEUVOTE', 'chamber': 'Congrès', 'source_external_id': sid,
                         'source_locator': locator + '#lieuVote'})
                orgs.add(institution)
            source_dossier = (raw.get('objet') or {}).get('dossierLegislatif') or {}
            dossier = 'DV:DOSSIER:AN:' + str(source_dossier.get('dossierRef') or '')
            if not self.db.execute("SELECT 1 FROM records WHERE kind='dossier' AND id=?", (dossier,)).fetchone():
                dossier = None
            self.put('scrutin', scrutiny_id,
                     {'ballot_number': number, 'official_title': title, 'occurred_on': when,
                      'date_precision': 'day', 'legislature': str(legislature),
                      'institution_external_id': institution if institution in orgs else None,
                      'procedure': str((raw.get('typeVote') or {}).get('libelleTypeVote') or ''),
                      'result_label': str((raw.get('sort') or {}).get('libelle') or ''),
                      'official_url': ('https://www.assemblee-nationale.fr/dyn/congres_mars_2024'
                                       if congress and when == '2024-03-04' else
                                       f'https://www.assemblee-nationale.fr/dyn/{legislature}/scrutins/{number}'),
                      'count_for': integer('pour'), 'count_against': integer('contre'),
                      'count_abstention': integer('abstentions'),
                      'nominative_status': 'available' if observations else 'not_published',
                      'source_external_id': sid, 'source_locator': locator,
                      'dossier_external_id': dossier,
                      'text_version_external_id': None,
                      'coverage_label': 'Version exacte du texte non renseignée ; relevé nominatif selon la source.'})
            for actor, category, group in observations:
                if duplicates[actor] != 1:
                    self.losses['duplicate_actor_in_scrutin'] += 1
                    continue
                pid = 'DV:PERSON:AN:' + actor
                if pid not in people and congress:
                    identity = self.db.execute('SELECT data FROM actors WHERE id=?', (actor,)).fetchone()
                    if identity:
                        self.put('person', pid, json.loads(identity[0]))
                        people.add(pid)
                if pid not in people:
                    self.losses['vote_actor_unresolved'] += 1
                    continue
                gid = 'DV:ORG:AN:' + group
                self.put('vote', 'DV:VOTE:AN:' + uid + ':' + actor,
                         {'scrutin_external_id': scrutiny_id, 'person_external_id': pid,
                          'position': POSITIONS[category], 'source_category': category,
                          'group_external_id_at_vote': gid if gid in orgs else None,
                          'group_source_ref': group, 'recorded_on': when, 'recorded_at': None,
                          'source_external_id': sid, 'source_locator': locator})
                if group and gid not in orgs:
                    self.losses['vote_group_unresolved_retained_as_source_ref'] += 1
            count += 1
            if count % 1000 == 0:
                print(canonical({'source': name, 'scrutins_processed': count}), flush=True)
        self.db.commit()

    def finish(self):
        manifest = {'schema': 'dv-public-export-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                    'source_receipts': self.receipts, 'losses': dict(self.losses), 'batches': [],
                    'scope': self.scope,
                    'editorial_authorization': False}
        (self.output / 'batches').mkdir()
        for kind in TYPE_ORDER:
            cursor = self.db.execute('SELECT id,data FROM records WHERE kind=? ORDER BY id', (kind,))
            index = 0
            while rows := cursor.fetchmany(1000):
                records = []
                for identifier, encoded in rows:
                    data = json.loads(encoded)
                    records.append({'record_type': kind, 'record_id': identifier,
                                    'revision': 'v1-' + digest(data)[:24], 'operation': 'upsert',
                                    'publication_circuit': 'factual', 'is_test': False, 'data': data})
                content = digest(records)
                batch_id = f'DV-V1-{kind}-{index:05d}-{content[:12]}'
                payload = {'bridge_schema': SCHEMA, 'batch_id': batch_id, 'idempotency_key': batch_id,
                           'generated_at': manifest['created_at'], 'records': records}
                relative = f'batches/{kind}-{index:05d}.json'
                binary = canonical(payload).encode('utf-8')
                (self.output / relative).write_bytes(binary)
                manifest['batches'].append({'file': relative, 'sha256': hashlib.sha256(binary).hexdigest(),
                                           'records': len(records), 'type': kind})
                index += 1
        manifest['counts'] = dict(self.db.execute('SELECT kind,COUNT(*) FROM records GROUP BY kind'))
        self.db.close()
        (self.output / 'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(canonical({'counts': manifest['counts'], 'losses': manifest['losses'],
                         'batches': len(manifest['batches'])}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--with-documents', action='store_true')
    args = parser.parse_args()
    export = Exporter(args.sources.resolve(), args.output.resolve())
    for legislature in (16, 17):
        export.actors(f'AN-AMO50-{legislature}')
    export.actors('AN-AMO30-17')
    export.actors('AN-AMO10-17')
    export.finalize_actors()
    if args.with_documents:
        for legislature in (16, 17):
            export.documents(f'AN-DOSSIERS-{legislature}')
    for legislature in (16, 17):
        export.votes(f'AN-SCRUTINS-{legislature}', legislature)
    export.finish()


if __name__ == '__main__':
    main()
