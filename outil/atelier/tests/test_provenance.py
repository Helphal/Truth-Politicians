"""New SYNTH originals, not repaired historical outcomes; no provider."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from atelier.services.canonical import sha256_digest
from atelier.services.provenance import (build_provenance_report, validate_report,
    project_affiliations_v2, validate_certificate_shape, _html_records)


def claims(value, location=''):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from claims(value[key], location + '/' + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from claims(item, location + '/' + str(index))
    elif value is not None and value != '':
        yield {'id': sha256_digest([location, value])[:24], 'location': location, 'value': value}


def archive(name='SYNTH Alice', uid='PA1', end=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as writer:
        writer.writestr('acteur/' + uid + '.json', json.dumps({'acteur': {
            'uid': uid, 'etatCivil': {'ident': {'prenom': name.split(' ', 1)[0], 'nom': name.split(' ', 1)[1]}}}}))
        writer.writestr('organe/PO1.json', json.dumps({'organe': {
            'uid': 'PO1', 'libelle': 'SYNTH Groupe', 'codeType': 'GP',
            'viMoDe': {'dateDebut': '2022-06-28', 'dateFin': None}}}))
        writer.writestr('mandat/PM1.json', json.dumps({'mandat': {'uid': 'PM1',
            'acteurRef': uid, 'typeOrgane': 'GP', 'organes': {'organeRef': 'PO1'},
            'dateDebut': '2022-06-29', 'dateFin': end}}))
    return buffer.getvalue()


AN_HTML = '''<html><title>Analyse du scrutin n°3289 - 16e législature - Assemblée nationale</title><h1>Scrutin n° 3289 du 30 janvier 2024</h1>
<h3><a href="/dyn/org/PO1">SYNTH Groupe</a></h3>
<ul><li><span class="h6">Pour</span><ul><li data-acteur-id="PA1 - PM1">
<a href="/dyn/deputes/PA1">M. SYNTH Alice</a></li></ul></li></ul></html>'''.encode()
SENATE_HTML = '''<html><title>Scrutin n°178 - séance du 29 janvier 2026 - Sénat</title><h1>Scrutin n° 178 - séance du 29 janvier 2026</h1>
<div id="accordion-collapse-scrutin-SOC"><ul><li><b>Contre</b> : M. SYNTH Lozach</li></ul></div>
<h3>Ont voté contre</h3><a href="/senateur/lozach_08012n.html">SYNTH Lozach</a></html>'''.encode()


class ProvenanceTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.descriptors, refs = [], []
        for source, name, data, media in [('SYNTH-AMO', 'original.zip', archive(), 'application/zip'),
                                        ('SYNTH-VOTE', 'original.html', AN_HTML, 'text/html')]:
            (self.root/name).write_bytes(data)
            ref = {'source_id': source, 'version_id': 'received-r1', 'sha256': hashlib.sha256(data).hexdigest()}
            refs.append(ref)
            self.descriptors.append({**ref, 'original_path': name, 'media_type': media, 'byte_size': len(data)})
        self.amo, self.vote = refs
        self.payload = {'catalogue': {'persons': [{'id': 'AN:PERSON:PA1', 'display_name': 'SYNTH Alice',
            'institutional_ids': {'assemblee_nationale': 'PA1'}}],
            'organizations': [{'id': 'AN:ORG:PO1', 'name': 'SYNTH Groupe', 'kind': 'parliamentary_group'}],
            'vote_observations': [{'scrutiny_id': 'AN:16:SCRUTIN:3289', 'person_id': 'AN:PERSON:PA1', 'normalized_category': 'for',
                'source_category': 'pours', 'affiliation_at_event_id': 'AN:ORG:PO1', 'source_version': self.vote}],
            'affiliations': [{'person_id': 'AN:PERSON:PA1', 'organization_id': 'AN:ORG:PO1',
                'status': 'observed_in_official_vote_list', 'starts_on': '2024-01-30', 'ends_on': '2024-01-30',
                'observed_on': '2024-01-30', 'source_version': self.vote},
                {'person_id': 'AN:PERSON:PA1', 'organization_id': 'AN:ORG:PO1', 'status': 'official_group_mandate',
                 'starts_on': '2022-06-29', 'ends_on': None, 'source_version': self.amo}]}}
        self.snapshot = self.freeze()

    def freeze(self):
        content = {'public_payload': deepcopy(self.payload)}
        return {'object': 'SYNTH', 'revision': 1, 'content': content,
            'sources': [{'reference': self.amo}, {'reference': self.vote}], 'claims': list(claims(content))}

    def build(self, snapshot=None, descriptors=None):
        return build_provenance_report(snapshot or self.snapshot,
            received_sources=self.descriptors if descriptors is None else descriptors, source_root=self.root)

    def cert(self, suffix, report=None):
        return next(c for c in (report or self.build())['certificates'] if c['location'].endswith(suffix))

    def test_original_record_is_recomputed_not_snapshot_self_attested(self):
        report = self.build()
        name = self.cert('/persons/0/display_name', report)
        self.assertEqual(name['status'], 'proven')
        self.assertEqual(name['raw_value'], {'prenom': 'SYNTH', 'nom': 'Alice'})
        self.assertEqual(name['record_locator']['member'], 'acteur/PA1.json')
        self.assertTrue(validate_report(self.snapshot, report, received_sources=self.descriptors, source_root=self.root))

    def test_raw_value_tampered_even_with_recomputed_self_digest_is_rejected(self):
        report = self.build()
        cert = self.cert('/persons/0/display_name', report)
        cert['raw_value'] = 'SYNTH altered'
        cert['certificate_id'] = sha256_digest({k:v for k,v in cert.items() if k != 'certificate_id'})
        report['report_digest'] = sha256_digest({k:v for k,v in report.items() if k != 'report_digest'})
        with self.assertRaisesMessage(ValidationError, 'original_recomputation_mismatch'):
            validate_report(self.snapshot, report, received_sources=self.descriptors, source_root=self.root)

    def test_wrong_institutional_identifier_not_proven(self):
        self.payload['catalogue']['persons'][0]['id'] = 'AN:PERSON:PA999'
        self.assertEqual(self.cert('/persons/0/display_name', self.build(self.freeze()))['status'], 'missing')

    def test_wrong_version_not_proven(self):
        changed = deepcopy(self.descriptors)
        changed[0]['version_id'] = 'other-version'
        # HTML can corroborate a name, but the explicit AMO source metadata cannot.
        self.assertEqual(self.cert('/affiliations/1/source_version/version_id', self.build(descriptors=changed))['status'], 'missing')

    def test_wrong_sha_refused(self):
        (self.root/'original.zip').write_bytes(archive('SYNTH Altered'))
        report = self.build()
        self.assertIn('source_sha_or_size_mismatch', [s['extraction_state'] for s in report['sources']])
        self.assertEqual(self.cert('/affiliations/1/starts_on', report)['status'], 'missing')

    def test_missing_original_refused(self):
        (self.root/'original.zip').unlink()
        self.assertEqual(self.cert('/affiliations/1/starts_on')['status'], 'missing')

    def test_extraction_nondeterministic_is_refused(self):
        from atelier.services import provenance
        original = provenance._extract
        calls = [0]
        def nondeterministic(request, record):
            value = original(request, record)
            if value and request.get('field') == 'display_name':
                calls[0] += 1
                return value[0], 'SYNTH ' + str(calls[0])
            return value
        with patch.object(provenance, '_extract', nondeterministic):
            self.assertEqual(self.cert('/persons/0/display_name')['status'], 'ambiguous')

    def test_two_received_sources_conflict_not_favorable(self):
        data = archive('SYNTH Contradictory')
        (self.root/'conflict.zip').write_bytes(data)
        ref = {'source_id': 'SYNTH-OTHER', 'version_id': 'r1', 'sha256': hashlib.sha256(data).hexdigest()}
        descriptors = self.descriptors + [{**ref, 'original_path': 'conflict.zip', 'media_type': 'application/zip'}]
        snap = deepcopy(self.snapshot)
        snap['sources'].append({'reference': ref})
        certificate = self.cert('/persons/0/display_name', self.build(snap, descriptors))
        self.assertEqual(certificate['status'], 'conflict')
        self.assertEqual(len({r['source_id'] for r in certificate['supporting_records']}), 3)

    def test_snapshot_date_is_not_affiliation_end_j48_regression(self):
        cert = self.cert('/affiliations/0/ends_on')
        self.assertEqual(cert['status'], 'ambiguous')
        self.assertIn('observation_is_not_membership_boundary', cert['reasons'])
        self.assertEqual(self.cert('/affiliations/0/observed_on')['status'], 'proven')

    def test_senate_j49_observation_is_not_exit(self):
        records = _html_records(SENATE_HTML)
        self.assertEqual(records[0]['group_id'], 'SENAT:GROUP:SER')
        self.assertEqual(records[0]['observed_on'], '2026-01-29')
        self.assertNotIn('ends_on', records[0])

    def test_person_institutional_field_must_match_institution(self):
        self.payload['catalogue']['persons'][0]['institutional_ids']['senat'] = 'PA1'
        report = self.build(self.freeze())
        self.assertEqual(self.cert('/persons/0/institutional_ids/senat', report)['status'], 'missing')
        self.assertEqual(self.cert('/persons/0/institutional_ids/assemblee_nationale', report)['status'], 'proven')

    def test_senate_contradictory_group_is_not_certified(self):
        data = SENATE_HTML.replace(b'</html>', b'<div id="accordion-collapse-scrutin-RDSE"><ul><li><b>Contre</b> : M. SYNTH Lozach</li></ul></div></html>')
        (self.root/'original.html').write_bytes(data)
        self.vote.update(sha256=hashlib.sha256(data).hexdigest())
        self.descriptors[1].update(sha256=self.vote['sha256'], byte_size=len(data))
        self.payload = {'catalogue': {'vote_observations': [{'person_id':'SENAT:PERSON:08012N',
            'scrutiny_id':'SENAT:2025-2026:SCRUTIN:178', 'affiliation_at_event_id':'SENAT:GROUP:SER',
            'source_version':self.vote}], 'affiliations': [{'person_id':'SENAT:PERSON:08012N',
            'organization_id':'SENAT:GROUP:SER','status':'membership_attested_at_scrutiny',
            'starts_on':'2026-01-29','ends_on':'2026-01-29','observed_on':'2026-01-29','source_version':self.vote}]}}
        report = self.build(self.freeze())
        for path in ('/vote_observations/0/affiliation_at_event_id','/affiliations/0/organization_id'):
            cert = self.cert(path, report)
            self.assertEqual(cert['status'], 'conflict')
            self.assertIn('source_group_conflict', cert['reasons'])

    def test_arbitrary_json_records_are_not_institutional_certificates(self):
        data = json.dumps({'records':[{'kind':'person','id':'AN:PERSON:PA1',
            'institutional_id':'PA1','name':'SYNTH Alice','raw_name':'SYNTH Alice','locator':{'self':'declared'}}]}).encode()
        (self.root/'self.json').write_bytes(data)
        descriptor = {**self.vote,'sha256':hashlib.sha256(data).hexdigest(), 'original_path':'self.json',
            'media_type':'application/json','byte_size':len(data)}
        snap = self.freeze()
        snap['sources'] = [{'reference': {k:descriptor[k] for k in ('source_id','version_id','sha256')}}]
        report = self.build(snap, descriptors=[descriptor])
        self.assertEqual(self.cert('/persons/0/display_name', report)['status'], 'missing')

    def test_vote_from_other_scrutiny_cannot_be_relabelled(self):
        self.payload['catalogue']['vote_observations'][0]['scrutiny_id'] = 'AN:16:SCRUTIN:9999'
        self.assertEqual(self.cert('/vote_observations/0/normalized_category', self.build(self.freeze()))['status'], 'missing')

    def test_same_number_in_wrong_legislature_cannot_be_relabelled(self):
        self.payload['catalogue']['vote_observations'][0]['scrutiny_id'] = 'AN:17:SCRUTIN:3289'
        self.assertEqual(self.cert('/vote_observations/0/normalized_category', self.build(self.freeze()))['status'], 'missing')

    def test_scrutiny_date_and_count_are_original_header_values(self):
        data = AN_HTML.replace(b'</h1>', b'</h1><div>Nombre de votants : 546</div>')
        (self.root/'original.html').write_bytes(data)
        self.vote.update(sha256=hashlib.sha256(data).hexdigest())
        self.descriptors[1].update(sha256=self.vote['sha256'], byte_size=len(data))
        self.payload['catalogue']['scrutinies'] = [{'scrutin_id':'AN:16:SCRUTIN:3289',
            'occurred_on':'2024-01-30', 'counts':{'voters':{'state':'known','value':546}}, 'source_version':self.vote}]
        report = self.build(self.freeze())
        self.assertEqual(self.cert('/scrutinies/0/counts/voters/value', report)['status'], 'proven')
        self.assertEqual(self.cert('/scrutinies/0/occurred_on', report)['status'], 'proven')

    def test_institutional_period_start_is_available_but_null_end_is_unknown(self):
        cert = self.cert('/affiliations/1/starts_on')
        self.assertEqual((cert['status'], cert['fact_type']), ('proven', 'affiliation_start'))
        projected = project_affiliations_v2(self.payload, provenance_report=self.build(), snapshot=self.snapshot,
            received_sources=self.descriptors, source_root=self.root)
        self.assertEqual(projected['catalogue']['affiliations'][1]['starts_on'], '2022-06-29')
        self.assertIsNone(projected['catalogue']['affiliations'][1]['ends_on'])
        self.assertEqual(projected['catalogue']['affiliations'][1]['ends_on_state'], 'unknown')

    def test_projection_preserves_history_without_mutating_input(self):
        before = sha256_digest(self.payload)
        projected = project_affiliations_v2(self.payload, provenance_report=self.build(), snapshot=self.snapshot,
            received_sources=self.descriptors, source_root=self.root)
        row = projected['catalogue']['affiliations'][0]
        self.assertIsNone(row['ends_on'])
        self.assertEqual(row['legacy_bounds']['ends_on'], '2024-01-30')
        self.assertEqual(row['observed_on'], '2024-01-30')
        self.assertEqual(sha256_digest(self.payload), before)

    def test_projection_refuses_certificate_without_recomputable_snapshot(self):
        with self.assertRaisesMessage(ValidationError, 'projection_verified_snapshot_required'):
            project_affiliations_v2(self.payload, provenance_report=self.build())

    def test_unknown_is_not_absence_or_party_inference(self):
        projected = project_affiliations_v2(self.payload)
        row = projected['catalogue']['affiliations'][0]
        self.assertEqual(row['starts_on_state'], 'unknown')
        self.assertEqual(row['observed_on_state'], 'unknown')
        self.assertNotIn('party_id', row)

    def test_names_cannot_bind_vote_to_wrong_id(self):
        changed = deepcopy(self.payload)
        changed['catalogue']['vote_observations'][0]['person_id'] = 'AN:PERSON:PA999'
        content = {'public_payload': changed}
        snap = {**self.snapshot, 'content': content, 'claims': list(claims(content))}
        self.assertEqual(self.cert('/vote_observations/0/normalized_category', self.build(snap))['status'], 'missing')

    def test_group_does_not_imply_unanimity(self):
        cert = self.cert('/vote_observations/0/affiliation_at_event_id')
        self.assertEqual(cert['status'], 'proven')
        self.assertNotIn('unanimous', json.dumps(cert))

    def test_result_bool_is_not_numeric_count(self):
        changed = deepcopy(self.snapshot)
        changed['claims'][0]['value'] = True
        with self.assertRaisesMessage(ValidationError, 'claim_binding_mismatch'):
            self.build(changed)

    def test_certificate_rule_tamper_is_refused(self):
        cert = self.cert('/persons/0/id')
        cert['extraction_rule'] = 'arbitrary-python/1'
        with self.assertRaisesMessage(ValidationError, 'rule_invalid'):
            validate_certificate_shape(cert)

    def test_claim_missing_from_content_refused(self):
        snap = deepcopy(self.snapshot)
        snap['claims'].append({'id': 'bad', 'location': '/public_payload/not_here', 'value': 'SYNTH'})
        with self.assertRaisesMessage(ValidationError, 'claim_binding_mismatch'):
            self.build(snap)

    def test_source_path_escape_and_credentials_are_not_opened(self):
        changed = deepcopy(self.descriptors)
        changed[0]['original_path'] = '../auth.json'
        report = self.build(descriptors=changed)
        self.assertIn('sensitive_path_refused', [s['extraction_state'] for s in report['sources']])

    def test_received_report_replay_after_source_change_refused(self):
        report = self.build()
        (self.root/'original.html').write_bytes(AN_HTML.replace(b'Pour', b'Contre'))
        with self.assertRaisesMessage(ValidationError, 'original_recomputation_mismatch'):
            validate_report(self.snapshot, report, received_sources=self.descriptors, source_root=self.root)


class AppendOnlyProofTests(TestCase):
    def setUp(self):
        from atelier.models import EditorialDossier, EditorialRevision, ProofArtifact
        dossier = EditorialDossier.objects.create(stable_id='SYNTH-PROOF', content_kind='official_vote', established_kind='official_vote')
        self.revision = EditorialRevision.objects.create(dossier=dossier, revision=1, title='SYNTH', short_answer='SYNTH',
            main_caveat='SYNTH', reasoning='SYNTH', public_payload={}, snapshot_digest=sha256_digest({}))
        self.snapshot = {'object': dossier.stable_id, 'revision': 1, 'content': {'public_payload': {}}}
        self.fingerprint = sha256_digest(self.snapshot)
        self.payload = {'schema': 'dv.preflight/1', 'fingerprint': self.fingerprint, 'state': 'blocked'}
        self.record = ProofArtifact.objects.create(revision=self.revision, revision_fingerprint=self.fingerprint,
            kind='preflight', schema='dv.preflight/1', payload=self.payload, payload_digest=sha256_digest(self.payload))

    def test_persisted_receipt_binding_recomputed(self):
        self.assertTrue(self.record.validate_binding(self.snapshot))
        with self.assertRaisesMessage(ValidationError, 'binding_stale'):
            self.record.validate_binding({**self.snapshot, 'revision': 2})

    def test_update_and_delete_refused(self):
        from atelier.models import ProofArtifact
        for change in [lambda:self.record.save(), lambda:self.record.delete(),
                lambda:ProofArtifact.objects.filter(pk=self.record.pk).update(kind='semantic'),
                lambda:ProofArtifact.objects.filter(pk=self.record.pk).delete()]:
            with self.assertRaisesMessage(ValidationError, 'immutable'):
                change()

    def test_self_declared_digest_does_not_match_altered_payload(self):
        from atelier.models import ProofArtifact
        with self.assertRaisesMessage(ValidationError, 'payload_digest_mismatch'):
            ProofArtifact.objects.create(revision=self.revision, revision_fingerprint=self.fingerprint,
                kind='semantic', schema='dv.semantic/1', payload={'fingerprint':self.fingerprint,'status':'favorable'},
                payload_digest=sha256_digest(self.payload))

    def test_no_actor_or_human_review_is_created(self):
        from atelier.models import HumanReview
        self.assertIsNone(self.record.actor_id)
        self.assertEqual(HumanReview.objects.count(), 0)
