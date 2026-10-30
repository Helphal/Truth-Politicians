"""Fixtures SYNTH isolées ; aucune donnée de test n'est livrée au corpus réel."""
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from export_public_v1 import Exporter


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


class ExportTests(unittest.TestCase):
    def test_document_is_preserved_with_exact_title_and_dossier_reference(self):
        title = 'SYNTH titre officiel ' + 'long ' * 130
        self.archive('SYNTH-DOCUMENTS', [
            ('dossierParlementaire', {'uid': 'DLTEST', 'legislature': '17',
                                      'titreDossier': {'titre': 'SYNTH dossier', 'titreChemin': 'synth-dossier'}}),
            ('document', {'uid': 'PIONANR5L17BTEST', 'legislature': '17', 'dossierRef': 'DLTEST',
                          'titres': {'titrePrincipal': title}, 'cycleDeVie': {'chrono': {'dateDepot': '2024-07-10'}}})])
        self.export.documents('SYNTH-DOCUMENTS')
        text = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='text'").fetchone()[0])
        version = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='text_version'").fetchone()[0])
        self.assertEqual(text['official_title'], title)
        self.assertEqual(text['dossier_external_id'], 'DV:DOSSIER:AN:DLTEST')
        self.assertEqual(version['occurred_on'], '2024-07-10')
        self.assertIsNone(version['institution_external_id'])
        self.assertEqual(self.export.db.execute("SELECT COUNT(*) FROM records WHERE kind='scrutin'").fetchone()[0], 0)

    def setUp(self):
        clock = patch('export_public_v1.datetime', FixtureClock)
        clock.start()
        self.addCleanup(clock.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix='DV-SYNTH-EXPORT-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.export = Exporter(self.sources, self.root / 'output')
        self.addCleanup(self.export.db.close)

    def archive(self, name, records):
        path = self.sources / (name + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for index, (kind, record) in enumerate(records):
                archive.writestr(f'{index}.json', json.dumps({kind: record}))
        receipt = {'status': 'downloaded', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'url': 'https://example.org/SYNTH.zip', 'fetched_at': '2026-09-22T00:00:00Z'}
        (self.sources / (name + '.receipt.json')).write_text(json.dumps(receipt), encoding='utf-8')

    def actors(self):
        self.archive('SYNTH-ACTORS', [
            ('acteur', {'uid': 'PA1', 'etatCivil': {'ident': {'prenom': 'SYNTH', 'nom': 'Person'}},
                        'adresses': {'adresse': 'PRIVATE_ADDRESS_EXCLUDED'}, 'profession': 'EXCLUDED'}),
            ('organe', {'uid': 'PO1', 'codeType': 'ASSEMBLEE', 'libelle': 'SYNTH institution'}),
            ('organe', {'uid': 'PO2', 'codeType': 'GP', 'libelle': 'SYNTH groupe'}),
            ('mandat', {'uid': 'PM1', 'acteurRef': 'PA1', 'typeOrgane': 'ASSEMBLEE', 'legislature': '17',
                       'dateDebut': '2024-07-01', 'dateFin': None, 'organes': {'organeRef': 'PO1'}}),
        ])
        self.export.actors('SYNTH-ACTORS')
        self.export.finalize_actors()

    def scrutin(self, duplicate=False):
        nom = {'pours': {'votant': {'acteurRef': 'PA1'}}}
        if duplicate:
            nom['contres'] = {'votant': {'acteurRef': 'PA1'}}
        return {'uid': 'VTANR5L17V1', 'numero': '1', 'dateScrutin': '2024-09-01', 'titre': 'SYNTH scrutiny',
                'organeRef': 'PO1', 'syntheseVote': {'decompte': {'pour': '0', 'contre': None}},
                'ventilationVotes': {'organe': {'groupes': {'groupe': {'organeRef': 'PO2',
                                        'vote': {'decompteNominatif': nom}}}}}}

    def test_positive_privacy_projection_and_unknown_end_preserved(self):
        self.actors()
        rows = list(self.export.db.execute('SELECT data FROM records'))
        serialized = json.dumps(rows)
        self.assertNotIn('PRIVATE_ADDRESS_EXCLUDED', serialized)
        self.assertNotIn('profession', serialized)
        data = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='mandate'").fetchone()[0])
        self.assertIsNone(data['ends_on'])
        self.assertNotIn('current_attested_on', data)

    def test_votes_keep_day_zero_unknown_group_and_no_artificial_text(self):
        self.actors()
        self.archive('SYNTH-VOTES', [('scrutin', self.scrutin())])
        self.export.votes('SYNTH-VOTES', 17)
        vote = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='vote'").fetchone()[0])
        scrutin = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='scrutin'").fetchone()[0])
        self.assertIsNone(vote['recorded_at'])
        self.assertEqual(vote['recorded_on'], '2024-09-01')
        self.assertEqual(vote['group_external_id_at_vote'], 'DV:ORG:AN:PO2')
        self.assertEqual(scrutin['count_for'], 0)
        self.assertIsNone(scrutin['count_against'])
        self.assertIsNone(scrutin['text_version_external_id'])

    def test_duplicate_observation_is_excluded_with_reason(self):
        self.actors()
        self.archive('SYNTH-VOTES', [('scrutin', self.scrutin(duplicate=True))])
        self.export.votes('SYNTH-VOTES', 17)
        self.assertEqual(self.export.db.execute("SELECT COUNT(*) FROM records WHERE kind='vote'").fetchone()[0], 0)
        self.assertEqual(self.export.losses['duplicate_actor_in_scrutin'], 2)

    def test_source_hash_mismatch_fails_closed(self):
        self.archive('SYNTH-HASH', [])
        with (self.sources / 'SYNTH-HASH.zip').open('ab') as file:
            file.write(b'TEST tamper')
        with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
            self.export.source('SYNTH-HASH')

    def test_composite_mandate_inherits_only_explicit_parent_id(self):
        self.archive('AN-AMO10-17', [
            ('acteur', {'uid': 'PA3', 'etatCivil': {'ident': {'prenom': 'SYNTH', 'nom': 'Composite'}},
                        'mandats': {'mandat': {'uid': 'PM3', 'typeOrgane': 'ASSEMBLEE', 'legislature': '17',
                                  'dateDebut': '2024-07-01', 'dateFin': None, 'organes': {'organeRef': 'PO3'}}}}),
            ('organe', {'uid': 'PO3', 'codeType': 'ASSEMBLEE', 'libelle': 'SYNTH institution'}),
        ])
        self.export.actors('AN-AMO10-17')
        self.export.finalize_actors()
        record = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='mandate'").fetchone()[0])
        self.assertEqual(record['person_external_id'], 'DV:PERSON:AN:PA3')
        self.assertEqual(record['current_attested_on'], '2026-09-22')

    def test_source_attestation_survives_later_execution_dates(self):
        for day in ('2026-09-22', '2026-09-23', '2026-09-25', '2027-01-01'):
            with self.subTest(execution_date=day):
                instant = datetime.fromisoformat(day + 'T12:00:00+00:00')

                class ExecutionClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return instant

                with patch('export_public_v1.datetime', ExecutionClock):
                    self.export = Exporter(self.sources, self.root / ('output-' + day))
                self.addCleanup(self.export.db.close)
                self.assertEqual(self.export.today, day)
                self.test_composite_mandate_inherits_only_explicit_parent_id()

    def test_congress_preserves_separate_institution_and_sourced_identity(self):
        self.actors()
        self.archive('SYNTH-SENATOR', [('acteur', {'uid': 'PA9',
                     'etatCivil': {'ident': {'prenom': 'SYNTH', 'nom': 'Senator'}}})])
        self.export.actors('SYNTH-SENATOR')
        scrutiny = self.scrutin()
        scrutiny.update(uid='VTCGR5L16V1', dateScrutin='2024-03-04', lieuVote='Congrès')
        scrutiny['ventilationVotes']['organe']['groupes']['groupe']['vote']['decompteNominatif'] = {
            'pour': {'votant': {'acteurRef': 'PA9'}}}
        self.archive('SYNTH-CONGRESS', [('scrutin', scrutiny)])
        self.export.votes('SYNTH-CONGRESS', 16)
        data = json.loads(self.export.db.execute("SELECT data FROM records WHERE kind='scrutin'").fetchone()[0])
        self.assertEqual(data['institution_external_id'], 'DV:INSTITUTION:CONGRES')
        self.assertIn('congres_mars_2024', data['official_url'])
        self.assertTrue(self.export.db.execute("SELECT 1 FROM records WHERE kind='person' AND id='DV:PERSON:AN:PA9'").fetchone())
        self.assertEqual(self.export.db.execute("SELECT COUNT(*) FROM records WHERE kind='vote'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
