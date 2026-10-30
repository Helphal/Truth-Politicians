"""Server routing of closed v2 projection; fresh SYNTH originals only."""
from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.test import override_settings

from atelier.models import Affiliation, Person, Organization, VoteObservation
from atelier.services import verification as verification
from atelier.services.canonical import sha256_digest
from atelier.services.proof_classification import classify_snapshot, validate_classification
from atelier.services.provenance import build_provenance_report
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_catalogue_provenance_v2 as fixture
from atelier.tests.test_provenance import archive, AN_HTML


class ClassificationV2Tests(AtelierTestCase):
    received = fixture.CatalogueProvenanceV2Tests.received
    bound_speech = fixture.CatalogueProvenanceV2Tests.bound_speech

    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory(prefix='dv-classification-v2-SYNTH-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.override = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        self.override.enable(); self.addCleanup(self.override.disable)
        self.amo = self.received('amo.zip', archive(), 'application/zip')
        self.vote = self.received('vote.html', AN_HTML, 'text/html')
        self.scrutiny = self.make_scrutiny(self.vote, source_identifier='3289')
        self.scrutiny.stable_id = 'AN:16:SCRUTIN:3289'; self.scrutiny.occurred_on = date(2024, 1, 30); self.scrutiny.save()
        self.person = Person.objects.create(stable_id='AN:PERSON:PA1', display_name='SYNTH Alice',
            institutional_ids={'assemblee_nationale': 'PA1'})
        self.group = Organization.objects.create(stable_id='AN:ORG:PO1', name='SYNTH Groupe', kind='parliamentary_group')
        Affiliation.objects.create(person=self.person, organization=self.group, status='official_group_mandate',
            starts_on=date(2022, 6, 29), source_document=self.amo)
        Affiliation.objects.create(person=self.person, organization=self.group, status='observed_in_official_vote_list',
            starts_on=self.scrutiny.occurred_on, ends_on=self.scrutiny.occurred_on,
            observed_on=self.scrutiny.occurred_on, source_document=self.vote)
        VoteObservation.objects.create(person=self.person, scrutiny=self.scrutiny, affiliation_at_event=self.group,
            source_category='pours', normalized_category='for', mapping_rule='SYNTH', source_document=self.vote)
        self.revision = self.bound_speech()
        self.snapshot = verification.snapshot(self.revision)

    def report(self, snapshot=None, unknowns=()):
        value = snapshot or self.snapshot
        return classify_snapshot(value, provenance_report=build_provenance_report(value), unknowns=unknowns)

    def row(self, kind, snapshot=None):
        return next((i, row) for i, row in enumerate((snapshot or self.snapshot)['content']['public_payload']['catalogue']['affiliations'])
                    if row['temporal_kind'] == kind)

    def test_event_observation_does_not_invent_period_and_amo_real_start_is_a(self):
        report = self.report(); validate_classification(self.snapshot, report)
        i, row = self.row('observed_at_scrutiny')
        prefix = f'/public_payload/catalogue/affiliations/{i}/'
        boundaries = [u for u in report['unknowns'] if u['location'] in {prefix+'starts_on', prefix+'ends_on'}]
        self.assertEqual(len(boundaries), 2)
        self.assertTrue(all(not u['necessary_for_conclusion'] and not u['absence_proven'] for u in boundaries))
        old = next(c for c in report['claims'] if c['location'] == prefix+'legacy_bounds/ends_on')
        self.assertEqual(old['layer'], 'unknown'); self.assertIsNone(old['certificate_id'])
        self.assertEqual(next(c for c in report['claims'] if c['location'] == prefix+'observed_on')['layer'], 'deterministic')
        j, period = self.row('institutional_period')
        real = next(c for c in report['claims'] if c['location'] == f'/public_payload/catalogue/affiliations/{j}/starts_on')
        self.assertEqual(real['layer'], 'deterministic'); self.assertEqual(real['provenance_status'], 'proven')
        unknown_end = next(u for u in report['unknowns'] if u['location'] == f'/public_payload/catalogue/affiliations/{j}/ends_on')
        self.assertTrue(unknown_end['necessary_for_conclusion'])

    def test_source_missing_removes_observation_exception(self):
        self.root.joinpath('vote.html').unlink()
        report = self.report(); i, _ = self.row('observed_at_scrutiny')
        unknown = next(u for u in report['unknowns'] if u['location'] == f'/public_payload/catalogue/affiliations/{i}/ends_on')
        self.assertTrue(unknown['necessary_for_conclusion'])

    def test_period_conclusion_remains_b_after_proven_observation(self):
        snapshot = deepcopy(self.snapshot)
        snapshot['content']['reasoning'] = 'SYNTH appartenance maintenue sans interruption pendant une période.'
        snapshot['claims'] = list(verification._scalars(snapshot['content']))
        report = self.report(snapshot)
        self.assertEqual(next(c for c in report['claims'] if c['location'] == '/reasoning')['layer'], 'semantic')

    def test_false_projection_marker_refused_by_closed_contract(self):
        snapshot = deepcopy(self.snapshot)
        i, row = self.row('observed_at_scrutiny', snapshot)
        row['legacy_bounds']['historical_only'] = False
        snapshot['claims'] = list(verification._scalars(snapshot['content']))
        with self.assertRaisesRegex(ValidationError, 'closed_public_contract_invalid'):
            self.report(snapshot)

    def test_known_external_date_cannot_be_reclassified_unknown(self):
        j, row = self.row('institutional_period')
        with self.assertRaisesRegex(ValidationError, 'known_external_fact_cannot_be_exempted'):
            self.report(unknowns=[{'location': f'/public_payload/catalogue/affiliations/{j}/starts_on',
                'unknown_kind': 'membership_bound_unknown', 'motif': 'SYNTH contournement refusé.', 'limits': ['SYNTH.']}])

    def test_client_cannot_choose_necessity_boolean_or_omit_d_null(self):
        report = self.report(); j, _ = self.row('institutional_period')
        unknown = next(u for u in report['unknowns'] if u['location'] == f'/public_payload/catalogue/affiliations/{j}/ends_on')
        unknown['necessary_for_conclusion'] = False
        report.pop('classification_digest'); report['classification_digest'] = sha256_digest(report)
        with self.assertRaisesRegex(ValidationError, 'binding_or_routing_changed'):
            validate_classification(self.snapshot, report)

