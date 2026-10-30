"""Temporal production raccord from NEW SYNTH originals; no historic DB."""
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from django.core.exceptions import ValidationError
from django.test import override_settings
from atelier.models import (Affiliation, AffiliationObservation, Person, Organization, VoteObservation,
    EvidencePassage, Declaration, Comparison, ComparisonAct, EditorialDossier, EditorialRevision)
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_provenance import archive, AN_HTML
from atelier.services.catalogue import build_catalogue, prepare_public_snapshot, binding_check
from atelier.services.contracts import validate_speech_vote_payload
from atelier.services.assembly_expansion import expand_materialized_votes
from atelier.services.canonical import sha256_digest


class CatalogueProvenanceV2Tests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.override = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.amo = self.received('amo.zip', archive(), 'application/zip')
        self.vote = self.received('vote.html', AN_HTML, 'text/html')
        self.scrutiny = self.make_scrutiny(self.vote, source_identifier='3289')
        self.scrutiny.stable_id = 'AN:16:SCRUTIN:3289'
        self.scrutiny.occurred_on = date(2024, 1, 30)
        self.scrutiny.save()
        self.person = Person.objects.create(stable_id='AN:PERSON:PA1', display_name='SYNTH Alice',
            institutional_ids={'assemblee_nationale':'PA1'})
        self.group = Organization.objects.create(stable_id='AN:ORG:PO1', name='SYNTH Groupe', kind='parliamentary_group')
        Affiliation.objects.create(person=self.person, organization=self.group, status='official_group_mandate',
            starts_on=date(2022, 6, 29), source_document=self.amo)
        Affiliation.objects.create(person=self.person, organization=self.group, status='observed_in_official_vote_list',
            starts_on=self.scrutiny.occurred_on, ends_on=self.scrutiny.occurred_on,
            observed_on=self.scrutiny.occurred_on, source_document=self.vote)
        VoteObservation.objects.create(person=self.person, scrutiny=self.scrutiny, affiliation_at_event=self.group,
            source_category='pours', normalized_category='for', mapping_rule='SYNTH', source_document=self.vote)

    def received(self, name, data, media):
        path = self.root/name
        path.write_bytes(data)
        document = self.make_document()
        document.sha256 = hashlib.sha256(data).hexdigest()
        document.media_type = media
        document.byte_size = len(data)
        document.original_path = str(path)
        document.save()
        return document

    def test_legacy_default_preserves_received_dates(self):
        legacy, refs = build_catalogue([self.scrutiny.stable_id])
        self.assertNotIn('contract', legacy)
        observed = next(a for a in legacy['affiliations'] if a['status']=='observed_in_official_vote_list')
        self.assertEqual(observed['starts_on'], '2024-01-30')
        self.assertEqual(observed['ends_on'], '2024-01-30')
        self.assertNotIn('starts_on_state', observed)

    def test_v2_recomputes_observation_and_actual_amo_start(self):
        catalogue, refs = build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2')
        self.assertEqual(catalogue['contract'], 'dv.catalogue/2')
        self.assertEqual(catalogue['scrutinies'][0]['submitted_text_id'], self.scrutiny.submitted_text.stable_id)
        observed = next(a for a in catalogue['affiliations'] if a['temporal_kind']=='observed_at_scrutiny')
        period = next(a for a in catalogue['affiliations'] if a['temporal_kind']=='institutional_period')
        self.assertEqual(observed['observed_on_state'], 'known')
        self.assertEqual(observed['observed_on'], '2024-01-30')
        self.assertIsNone(observed['starts_on'])
        self.assertIsNone(observed['ends_on'])
        self.assertTrue(observed['legacy_bounds']['historical_only'])
        self.assertEqual(period['starts_on'], '2022-06-29')
        self.assertEqual(period['starts_on_state'], 'known')
        self.assertIsNone(period['ends_on'])
        self.assertEqual(period['ends_on_state'], 'unknown')
        self.assertNotIn('membership_active', period)

    def test_unknown_profile_and_implicit_upgrade_are_refused(self):
        with self.assertRaisesMessage(ValidationError, 'profile_unsupported'):
            build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/999')
        with self.assertRaisesMessage(ValidationError, 'requires_explicit_public_profile'):
            prepare_public_snapshot('official_vote', {'scrutin_id':self.scrutiny.stable_id,
                'catalogue':{'contract':'dv.catalogue/2'}}, [])

    def bound_speech(self):
        passage = EvidencePassage.objects.create(document=self.vote, exact_text='SYNTH déclaration.',
            locator='SYNTH p1', public_url=self.vote.source.document_url)
        declaration = Declaration.objects.create(stable_id='SYNTH:DECL', speaker_person=self.person,
            speaker_label='SYNTH Alice', capacity='SYNTH', exact_quote=passage.exact_text,
            occurred_on=self.scrutiny.occurred_on, source_passage=passage, position_scope='personal')
        comparison = Comparison.objects.create(stable_id='SYNTH:COMP', declaration=declaration, result='inconclusive')
        ComparisonAct.objects.create(comparison=comparison, scrutiny=self.scrutiny,
            reason='SYNTH exact act for the declared test scope', chronological_relation='same_day')
        payload = self.payload('speech_vote')
        payload['public_contract_version'] = 'dv.content/0.6.0-provenance-draft.1'
        public, refs = prepare_public_snapshot('speech_vote', payload, [], comparison=comparison)
        validate_speech_vote_payload(public)
        dossier = EditorialDossier.objects.create(stable_id='SYNTH:V2', content_kind='speech_vote',
            established_kind='speech_vote', data_scope='real')
        revision = EditorialRevision.objects.create(dossier=dossier, revision=1, comparison=comparison,
            public_payload=public, source_snapshot=refs, snapshot_digest=sha256_digest(public))
        return revision

    def test_v2_binding_recomputed_and_forged_membership_end_refused(self):
        revision = self.bound_speech()
        self.assertTrue(binding_check(revision)[0])
        forged = deepcopy(revision.public_payload)
        observed = next(a for a in forged['catalogue']['affiliations'] if a['temporal_kind']=='observed_at_scrutiny')
        observed.update(ends_on='2024-01-30', ends_on_state='known')
        with self.assertRaises(ValidationError):
            validate_speech_vote_payload(forged)
        revision.public_payload = forged
        revision.snapshot_digest = sha256_digest(forged)
        self.assertFalse(binding_check(revision)[0])

    def test_submitted_text_identity_cannot_be_rebound_by_payload(self):
        revision = self.bound_speech()
        original = revision.public_payload['catalogue']['scrutinies'][0]['submitted_text_id']
        self.assertEqual(original, self.scrutiny.submitted_text.stable_id)
        revision.public_payload['catalogue']['scrutinies'][0]['submitted_text_id'] = 'SYNTH:OTHER-VERSION'
        self.assertFalse(binding_check(revision)[0])

    def test_original_tampering_invalidates_v2_binding(self):
        revision = self.bound_speech()
        self.assertTrue(binding_check(revision)[0])
        (self.root/'vote.html').write_bytes(AN_HTML.replace(b'Pour', b'Contre'))
        self.assertFalse(binding_check(revision)[0])

    def test_new_ingestion_persists_observation_without_invented_bounds(self):
        raw = {'numero':'3289','legislature':'16','dateScrutin':'2024-01-30','titre':self.scrutiny.official_title,
            'uid':'VTANR5L16V3289','ventilationVotes':{'organe':{'groupes':{'groupe':{'organeRef':'PO1',
                'vote':{'decompteNominatif':{'pours':{'votant':{'acteurRef':'PA1','mandatRef':'PM1'}}}}}}}}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive_file:
            archive_file.writestr('scrutin/VTANR5L16V3289.json', json.dumps({'scrutin':raw}))
        document = self.received('native.zip', buffer.getvalue(), 'application/zip')
        self.scrutiny.source_document = document
        self.scrutiny.save()
        legacy_count = Affiliation.objects.count()
        expand_materialized_votes(document, Path(document.original_path))
        expand_materialized_votes(document, Path(document.original_path))
        self.assertEqual(Affiliation.objects.count(), legacy_count)
        observation = AffiliationObservation.objects.get()
        self.assertEqual(observation.observed_on, self.scrutiny.occurred_on)
        self.assertNotIn('starts_on', observation.payload)
        self.assertNotIn('ends_on', observation.payload)
        catalogue, refs = build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2')
        current = next(a for a in catalogue['affiliations'] if a['source_version']['version_id']==document.version_id)
        self.assertEqual(current['observed_on_state'], 'known')
        self.assertEqual(current['known_at']['basis'], 'received_at_not_membership_date')
        self.assertIsNone(current['ends_on'])

    def test_v2_collective_does_not_infer_current_membership_from_null_amo_end(self):
        Affiliation.objects.filter(status='observed_in_official_vote_list').delete()
        with self.assertRaisesMessage(ValidationError, 'members_missing_or_excessive'):
            build_catalogue([self.scrutiny.stable_id], collective_group=self.group, profile='dv.catalogue/2')
        legacy, refs = build_catalogue([self.scrutiny.stable_id], collective_group=self.group)
        self.assertEqual(len(legacy['persons']), 1)
