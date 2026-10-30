"""SYNTH original documents for the new closed documentary rules."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from atelier.services.canonical import sha256_digest
from atelier.services.provenance import build_provenance_report, validate_report, _html_records
from .test_provenance import claims

SENATE = '''<html><title>Scrutin n°178 - séance du 29 janvier 2026 - Sénat</title>
<h1>Scrutin n°178 - séance du 29 janvier 2026</h1>
<div id="accordion-scrutin-SOC"><button data-bs-target="#accordion-collapse-scrutin-SOC">Groupe SYNTH Socialiste : 2 sénateurs</button></div>
<div id="accordion-collapse-scrutin-SOC"><ul><li><b>Contre</b> : M. SYNTH Lozach</li></ul></div>
<h3>Ont voté contre</h3><a href="/senateur/lozach_08012n.html">SYNTH Lozach</a></html>'''
DEBATE_URL = 'https://www.senat.fr/seances/s202601/s20260129/s20260129013.html'
CAPACITY = '''<html><p id="par_1573"><span class="orateur_nom">M. SYNTH Ministre,</span> ministre. SYNTH citation exacte.</p></html>'''
AMENDMENT_URL = 'https://www.senat.fr/amendements/2025-2026/298/Amdt_6.html'
AMENDMENT = '''<html><p>(n° 298, 297)</p><p>N° 6</p><h1>AMENDEMENT</h1><table><tr><td><b>Retiré</b></td></tr></table></html>'''


class DocumentaryDependenciesProvenanceTests(SimpleTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def report(self, public, html, url):
        data = html.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / (digest + '.html')
        path.write_bytes(data)
        ref = {'source_id': 'SYNTH-DOCUMENT', 'version_id': 'SYNTH-1', 'sha256': digest, 'public_url': url}
        public = deepcopy(public)
        public.setdefault('public_contract_version', 'dv.content/0.7.0-conclusion-draft.1')
        for item in public.get('object_observations', []):
            item['source_version'] = ref
        public.setdefault('evidence', [{'source_version': ref}])
        public.setdefault('qualification_material', {'declaration_source': {'document_version': 'SYNTH-1', 'sha256': digest}})
        content = {'public_payload': public}
        snapshot = {'scope': 'synthetic', 'content': content, 'sources': [{'reference': ref}], 'claims': list(claims(content))}
        descriptor = {**ref, 'original_path': str(path), 'byte_size': len(data), 'media_type': 'text/html'}
        report = build_provenance_report(snapshot, received_sources=[descriptor], source_root=self.root)
        validate_report(snapshot, report, received_sources=[descriptor], source_root=self.root)
        return report, snapshot, descriptor

    def certificate(self, report, suffix):
        return next(c for c in report['certificates'] if c['location'].endswith(suffix))

    def group_public(self, **changes):
        return {'catalogue': {'organizations': [{'id': 'SENAT:GROUP:SER', 'name': 'SYNTH Socialiste', 'kind': 'parliamentary_group', **changes}]}}

    def capacity_public(self, label='SYNTH Ministre', quote='SYNTH citation exacte.', capacity='ministre'):
        return {'actors': {'speaker': {'person_id': 'SYNTH-UNRESOLVED-NAME', 'label': label, 'capacity': capacity}},
                'attribution': {'speaker_label': label, 'capacity': capacity}, 'declaration_quote': quote}

    def object_public(self, **changes):
        return {'object_observations': [{'object_id': 'SENAT:2025-2026:AMENDMENT:298:6',
                'object_kind': 'amendment', 'status': 'withdrawn', 'official_title': 'AMENDEMENT', **changes}]}

    def test_senate_source_heading_certifies_group_without_dates(self):
        report, _, _ = self.report(self.group_public(), SENATE, 'https://www.senat.fr/scrutin-public/2025/scr2025-178.html')
        for field in ('id', 'name', 'kind'):
            certificate = self.certificate(report, '/organizations/0/' + field)
            self.assertEqual(certificate['status'], 'proven')
            self.assertEqual(certificate['record_locator']['source_group_id'], 'SOC')
        self.assertFalse(any(c['fact_type'].startswith('affiliation_') for c in report['certificates']))

    def test_group_is_not_party_and_unknown_dates_are_not_periods(self):
        report, _, _ = self.report(self.group_public(kind='party', valid_to='2026-01-29'), SENATE, 'https://www.senat.fr/scrutin-public/2025/scr2025-178.html')
        self.assertEqual(self.certificate(report, '/organizations/0/kind')['status'], 'conflict')
        self.assertEqual(self.certificate(report, '/organizations/0/valid_to')['status'], 'missing')

    def test_wrong_group_target_or_namespace_is_not_attested(self):
        for data in (SENATE.replace('data-bs-target="#accordion-collapse-scrutin-SOC"', 'data-bs-target="#accordion-collapse-scrutin-RDSE"'),
                     SENATE.replace('Groupe SYNTH Socialiste', 'Parti SYNTH Socialiste'),
                     SENATE.replace('Sénat', 'Assemblée nationale')):
            report, _, _ = self.report(self.group_public(), data, 'https://www.senat.fr/scrutin-public/2025/scr2025-178.html')
            self.assertEqual(self.certificate(report, '/organizations/0/name')['status'], 'missing')

    def test_conflicting_group_titles_are_not_selected_silently(self):
        extra = '<div id="accordion-scrutin-SOC"><button data-bs-target="#accordion-collapse-scrutin-SOC">Groupe SYNTH Autre : 2 sénateurs</button></div>'
        report, _, _ = self.report(self.group_public(), SENATE.replace('</html>', extra + '</html>'), 'https://www.senat.fr/scrutin-public/2025/scr2025-178.html')
        self.assertEqual(self.certificate(report, '/organizations/0/name')['status'], 'conflict')

    def test_capacity_is_bound_to_name_original_quote_and_record(self):
        report, _, _ = self.report(self.capacity_public(), CAPACITY, DEBATE_URL)
        certificates = [self.certificate(report, path) for path in ('/speaker/capacity', '/attribution/capacity')]
        for certificate in certificates:
            self.assertEqual(certificate['status'], 'proven')
            self.assertEqual(certificate['fact_type'], 'declared_actor_capacity')
            self.assertEqual(certificate['record_locator']['speaker_label'], 'SYNTH Ministre')
            self.assertEqual(certificate['record_id'], 'SENAT:CR:20260129:par_1573')

    def test_wrong_speaker_or_quote_or_source_namespace_is_not_capacity_proof(self):
        cases = [(self.capacity_public(label='SYNTH Autre'), CAPACITY, DEBATE_URL),
                 (self.capacity_public(quote='SYNTH citation absente.'), CAPACITY, DEBATE_URL),
                 (self.capacity_public(), CAPACITY, DEBATE_URL.replace('www.senat.fr', 'www.assemblee-nationale.fr')),
                 (self.capacity_public(), CAPACITY.replace('SYNTH citation exacte.', 'Autre citation.') + '<p>SYNTH citation exacte.</p>', DEBATE_URL)]
        for public, data, url in cases:
            report, _, _ = self.report(public, data, url)
            self.assertEqual(self.certificate(report, '/speaker/capacity')['status'], 'missing')

    def test_single_named_intervention_binds_its_continuation_paragraph(self):
        data = '<html><div id="intv_par_1573" class="intervenant"><p id="par_1573"><span class="orateur_nom">M. SYNTH Ministre,</span> ministre. SYNTH introduction.</p><p id="par_1574">SYNTH citation exacte.</p></div></html>'
        report, _, _ = self.report(self.capacity_public(), data, DEBATE_URL)
        certificate = self.certificate(report, '/speaker/capacity')
        self.assertEqual(certificate['status'], 'proven')
        self.assertGreater(certificate['record_locator']['declaration_context_locator']['end'],
                           certificate['record_locator']['end'])

    def test_neighbouring_or_multiple_named_interventions_cannot_borrow_capacity(self):
        named = '<p id="par_1573"><span class="orateur_nom">M. SYNTH Ministre,</span> ministre. SYNTH introduction.</p>'
        extra = '<p id="par_1574"><span class="orateur_nom">M. SYNTH Autre,</span> SYNTH citation exacte.</p>'
        for data in ('<html><div id="intv_par_1573" class="intervenant">' + named + '</div><div id="intv_par_1574" class="intervenant">' + extra + '</div></html>',
                     '<html><div id="intv_par_1573" class="intervenant">' + named + extra + '</div></html>'):
            report, _, _ = self.report(self.capacity_public(), data, DEBATE_URL)
            self.assertEqual(self.certificate(report, '/speaker/capacity')['status'], 'missing')

    def test_declared_name_never_resolves_uid(self):
        public = self.capacity_public()
        public['catalogue'] = {'persons': [{'id': 'SYNTH-UNRESOLVED-NAME', 'display_name': 'SYNTH Ministre', 'identity_status': 'resolved'}]}
        report, _, _ = self.report(public, CAPACITY, DEBATE_URL)
        self.assertEqual(self.certificate(report, '/speaker/capacity')['status'], 'proven')
        for field in ('id', 'display_name', 'identity_status'):
            self.assertEqual(self.certificate(report, '/persons/0/' + field)['status'], 'missing')

    def test_interpreted_capacity_and_legacy_label_remain_uncertified(self):
        public = self.capacity_public(capacity='SYNTH ministre au nom du Gouvernement')
        report, _, _ = self.report(public, CAPACITY, DEBATE_URL)
        self.assertFalse(any(c['location'].endswith('/capacity') for c in report['certificates']))
        public = self.capacity_public()
        public['public_contract_version'] = 'dv.content/0.6.0-provenance-draft.1'
        report, _, _ = self.report(public, CAPACITY, DEBATE_URL)
        self.assertFalse(any(c['location'].endswith('/capacity') for c in report['certificates']))

    def test_amendment_identity_and_withdrawal_are_same_original_record(self):
        report, _, _ = self.report(self.object_public(), AMENDMENT, AMENDMENT_URL)
        identity = self.certificate(report, '/object_observations/0/object_id')
        withdrawal = self.certificate(report, '/object_observations/0/status')
        self.assertEqual(identity['status'], 'proven')
        self.assertEqual(withdrawal['status'], 'proven')
        self.assertEqual(identity['record_id'], withdrawal['record_id'])
        self.assertEqual(identity['source_sha256'], withdrawal['source_sha256'])
        self.assertEqual(identity['fact_type'], 'amendment_identity')
        self.assertEqual(withdrawal['fact_type'], 'amendment_withdrawal')

    def test_wrong_amendment_number_or_bill_cannot_borrow_withdrawal(self):
        for url in (AMENDMENT_URL.replace('Amdt_6', 'Amdt_7'), AMENDMENT_URL.replace('/298/', '/299/')):
            report, _, _ = self.report(self.object_public(), AMENDMENT, url)
            self.assertEqual(self.certificate(report, '/object_observations/0/status')['status'], 'missing')
        report, _, _ = self.report(self.object_public(object_id='SENAT:2025-2026:AMENDMENT:298:13'), AMENDMENT, AMENDMENT_URL)
        self.assertEqual(self.certificate(report, '/object_observations/0/status')['status'], 'missing')

    def test_withdrawal_in_narration_or_unknown_status_is_not_official_status_cell(self):
        for data in (AMENDMENT.replace('<td><b>Retiré</b></td>', '<td>Inconnu</td><p>Retiré</p>'),
                     AMENDMENT.replace('<td><b>Retiré</b></td>', '<td>Non renseigné</td>')):
            report, _, _ = self.report(self.object_public(), data, AMENDMENT_URL)
            self.assertEqual(self.certificate(report, '/object_observations/0/status')['status'], 'missing')

    def test_conflicting_status_cells_are_not_selected_silently(self):
        data = AMENDMENT.replace('</tr>', '<td>Adopté</td></tr>')
        report, _, _ = self.report(self.object_public(), data, AMENDMENT_URL)
        self.assertEqual(self.certificate(report, '/object_observations/0/status')['status'], 'conflict')

    def test_document_url_change_is_detected_with_same_original_sha(self):
        report, snapshot, descriptor = self.report(self.object_public(), AMENDMENT, AMENDMENT_URL)
        changed = deepcopy(descriptor)
        changed['public_url'] = AMENDMENT_URL.replace('Amdt_6', 'Amdt_7')
        rebuilt = build_provenance_report(snapshot, received_sources=[changed], source_root=self.root)
        self.assertEqual(self.certificate(rebuilt, '/object_observations/0/status')['status'], 'missing')
        self.assertIn('source_public_url_binding_mismatch', self.certificate(rebuilt, '/object_observations/0/status')['reasons'])
