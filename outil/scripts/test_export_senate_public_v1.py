import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from export_senate_public_v1 import SenateExporter, copy_rows, pg_value


class SenateProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='DV-SYNTH-SENAT-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.export = SenateExporter(self.sources, self.root / 'output')
        self.addCleanup(self.export.db.close)

    def source(self, name, data):
        path = self.sources / name
        path.write_bytes(data)
        receipt = {'status': 'downloaded', 'sha256': hashlib.sha256(data).hexdigest(),
                   'fetched_at': '2026-09-22T00:00:00Z', 'url': 'https://example.org/SYNTH'}
        path.with_suffix(path.suffix + '.receipt.json').write_text(json.dumps(receipt), encoding='utf-8')

    def csv_source(self, name, columns, rows):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)
        self.source(name, ('% SYNTH provenance\n' + stream.getvalue()).encode('latin1'))

    def fixtures(self):
        self.csv_source('SENAT-ODSEN_GENERAL.csv',
                        ['Matricule', 'Prénom usuel', 'Nom usuel', 'État', 'Courrier électronique', 'Date naissance','Circonscription'],
                        [['TEST1', 'SYNTH', 'Person', 'ACTIF', 'PRIVATE-EXCLUDED', 'PRIVATE-DATE','SYNTH Territoire']])
        self.csv_source('SENAT-ODSEN_ELUSEN.csv',
                        ['Matricule', 'Date de début de mandat', 'Date de fin de mandat', 'Identifiant mandat'], [])
        self.csv_source('SENAT-ODSEN_HISTOGROUPES.csv',
                        ['Matricule', "Date de début d'appartenance", "Date de fin d'appartenance"], [])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('dosleg.sql', '''CREATE EXTENSION never_execute;
COPY scr (sesann, scrnum, scrint, scrdat, scrpou, scrcon, scrvot, scrsuf, scrvotsea, scrsufsea, scrpousea, scrconsea) FROM stdin;
2025\t1\tSYNTH vote\t2026-06-01 00:00:00\t0\t1\t1\t1\t1\t1\t0\t1
\\.
COPY votsen (sesann, scrnum, senmat, posvotcod, stavotidt) FROM stdin;
2025\t1\tTEST1\t2\t0
\\.
''')
        self.source('SENAT-dosleg.zip', stream.getvalue())

    def test_current_attestation_does_not_fabricate_start_or_private_fields(self):
        self.fixtures()
        self.export.actors()
        self.export.votes()
        records = list(self.export.db.execute('SELECT kind,data FROM records'))
        self.assertNotIn('PRIVATE-', json.dumps(records))
        mandate = json.loads(next(encoded for kind, encoded in records if kind == 'mandate'))
        self.assertIsNone(mandate['starts_on'])
        self.assertIsNone(mandate['ends_on'])
        self.assertEqual(mandate['current_attested_on'], '2026-09-22')
        self.assertEqual(mandate['territory'],'SYNTH Territoire')
        self.assertEqual(mandate['territory_attested_on'],'2026-09-22')
        self.assertIn('ODSEN_GENERAL',mandate['territory_source_external_id'])

    def test_current_territory_is_not_projected_onto_old_mandate(self):
        self.fixtures()
        self.csv_source('SENAT-ODSEN_ELUSEN.csv',
                        ['Matricule','Date de début de mandat','Date de fin de mandat','Identifiant mandat'],
                        [['TEST1','2022-01-01','2023-10-01','SYNTH-OLD']])
        self.export.actors()
        data=json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='mandate' AND id='SENAT:MANDATE:SYNTH-OLD'").fetchone()[0])
        self.assertNotIn('territory',data)

    def test_mandate_end_day_is_inclusive_and_next_mandate_stays_future(self):
        self.fixtures()
        self.csv_source('SENAT-ODSEN_ELUSEN.csv',
                        ['Matricule','Date de début de mandat','Date de fin de mandat','Identifiant mandat'],
                        [['TEST1','2020-10-01','2026-09-30','SYNTH-ENDING'],
                         ['TEST1','2026-10-01','','SYNTH-NEXT']])
        for path in self.sources.glob('*.receipt.json'):
            receipt=json.loads(path.read_text());receipt['fetched_at']='2026-09-30T12:00:00Z'
            path.write_text(json.dumps(receipt))
        self.export.actors()
        rows={rid:json.loads(data) for rid,data in self.export.db.execute("SELECT id,data FROM records WHERE kind='mandate'")}
        self.assertEqual(set(rows),{'SENAT:MANDATE:SYNTH-ENDING'})
        ending=rows['SENAT:MANDATE:SYNTH-ENDING']
        self.assertEqual(ending['starts_on'],'2020-10-01')
        self.assertEqual(ending['ends_on'],'2026-09-30')
        self.assertEqual(ending['current_attested_on'],'2026-09-30')
        self.assertEqual(ending['territory'],'SYNTH Territoire')
        self.assertEqual(self.export.losses['active_mandate_start_not_documented'],0)

    def test_following_day_preserves_history_and_uses_documented_new_start(self):
        self.fixtures()
        self.csv_source('SENAT-ODSEN_ELUSEN.csv',
                        ['Matricule','Date de début de mandat','Date de fin de mandat','Identifiant mandat'],
                        [['TEST1','2020-10-01','2026-09-30','SYNTH-ENDING'],
                         ['TEST1','2026-10-01','','SYNTH-NEXT']])
        for path in self.sources.glob('*.receipt.json'):
            receipt=json.loads(path.read_text());receipt['fetched_at']='2026-10-01T12:00:00Z'
            path.write_text(json.dumps(receipt))
        self.export.actors()
        rows={rid:json.loads(data) for rid,data in self.export.db.execute("SELECT id,data FROM records WHERE kind='mandate'")}
        self.assertEqual(set(rows),{'SENAT:MANDATE:SYNTH-ENDING','SENAT:MANDATE:SYNTH-NEXT'})
        self.assertIsNone(rows['SENAT:MANDATE:SYNTH-ENDING']['current_attested_on'])
        self.assertNotIn('territory',rows['SENAT:MANDATE:SYNTH-ENDING'])
        self.assertEqual(rows['SENAT:MANDATE:SYNTH-NEXT']['starts_on'],'2026-10-01')
        self.assertEqual(rows['SENAT:MANDATE:SYNTH-NEXT']['current_attested_on'],'2026-10-01')

    def test_ended_period_without_current_start_keeps_explicit_unknown(self):
        self.fixtures()
        self.csv_source('SENAT-ODSEN_ELUSEN.csv',
                        ['Matricule','Date de début de mandat','Date de fin de mandat','Identifiant mandat'],
                        [['TEST1','2020-10-01','2026-09-21','SYNTH-ENDED']])
        self.export.actors()
        rows={rid:json.loads(data) for rid,data in self.export.db.execute("SELECT id,data FROM records WHERE kind='mandate'")}
        self.assertEqual(set(rows),{'SENAT:MANDATE:SYNTH-ENDED','SENAT:CURRENT-ATTESTATION:TEST1'})
        self.assertIsNone(rows['SENAT:MANDATE:SYNTH-ENDED']['current_attested_on'])
        self.assertIsNone(rows['SENAT:CURRENT-ATTESTATION:TEST1']['starts_on'])
        self.assertEqual(self.export.losses['active_mandate_start_not_documented'],1)

    def test_nominal_position_no_group_inference_and_unknown_not_zero(self):
        self.fixtures()
        self.export.actors()
        self.export.votes()
        scrutiny = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='scrutin'").fetchone()[0])
        vote = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='vote'").fetchone()[0])
        self.assertEqual(scrutiny['count_for'], 0)
        self.assertIsNone(scrutiny['count_abstention'])
        self.assertEqual(scrutiny['occurred_on'], '2026-06-01')
        self.assertNotIn('occurred_at', scrutiny)
        self.assertEqual(vote['position'], 'against')
        self.assertIsNone(vote['group_external_id_at_vote'])

    def test_copy_null_escapes_and_opaque_sql(self):
        self.assertIsNone(pg_value(r'\N'))
        self.assertEqual(pg_value(r'a\tb\n\\N'), 'a\tb\n\\N')
        self.fixtures()
        rows = list(copy_rows(self.sources / 'SENAT-dosleg.zip', {'votsen'}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]['senmat'], 'TEST1')

    def test_tampered_source_is_rejected(self):
        self.fixtures()
        with (self.sources / 'SENAT-dosleg.zip').open('ab') as stream:
            stream.write(b'SYNTH tamper')
        with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
            self.export.official_source('SENAT-dosleg.zip')

    def test_identity_link_requires_explicit_official_profile_and_pinned_person(self):
        person = {'record_id': 'DV:PERSON:AN:PA1', 'data': {'display_name': 'SYNTH Same Name'}}
        payload = json.dumps({'records': [person]}).encode()
        manifest_dir = self.root / 'an'
        manifest_dir.mkdir()
        (manifest_dir / 'person.json').write_bytes(payload)
        manifest = {'batches': [{'type': 'person', 'file': 'person.json', 'sha256': hashlib.sha256(payload).hexdigest()}]}
        manifest_path = manifest_dir / 'MANIFEST.json'
        manifest_path.write_text(json.dumps(manifest))
        for name in ['AN-AMO50-16', 'AN-AMO50-17', 'AN-AMO30-17']:
            path = self.sources / (name + '.zip')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('actor.json', json.dumps({'acteur': {'uid': 'PA1',
                    'url': 'https://www.senat.fr/senateur/synth_same_name12345a.html'}}))
                archive.writestr('unpublished.json', json.dumps({'acteur': {'uid': 'PA2',
                    'url': 'https://www.senat.fr/senateur/synth_other12346b.html'}}))
            (self.sources / (name + '.receipt.json')).write_text(json.dumps({'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
        self.export.link_an_identities(manifest_path)
        self.assertEqual(self.export.person_id('12345A'), 'DV:PERSON:AN:PA1')
        self.assertEqual(self.export.person_id('12346B'), 'SENAT:PERSON:12346B')
        self.assertEqual(self.export.person_id('UNKNOWN'), 'SENAT:PERSON:UNKNOWN')


if __name__ == '__main__':
    unittest.main()
