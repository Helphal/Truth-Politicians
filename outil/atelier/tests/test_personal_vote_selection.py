"""Explicit personal vote selections use new SYNTH originals and ORM rows."""
from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.test import override_settings

from atelier.models import Person, VoteObservation
from atelier.services.catalogue import source_ref
from atelier.services.catalogue_v2 import personal_vote_selection_v07
from .support import AtelierTestCase
from .test_provenance_dependencies import SENATE


class PersonalVoteSelectionTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        setting = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        setting.enable()
        self.addCleanup(setting.disable)
        self.data = SENATE.encode('utf-8')
        self.path = self.root / (hashlib.sha256(self.data).hexdigest() + '.html')
        self.path.write_bytes(self.data)
        self.document = self.make_document()
        self.document.source.document_url = 'https://www.senat.fr/scrutin-public/2025/scr2025-178.html'
        self.document.source.save(update_fields=['document_url'])
        self.document.sha256 = hashlib.sha256(self.data).hexdigest()
        self.document.byte_size = len(self.data)
        self.document.original_path = str(self.path)
        self.document.media_type = 'text/html'
        self.document.save()
        self.scrutiny = self.make_scrutiny(self.document, source_identifier='178')
        self.scrutiny.stable_id = 'SENAT:2025-2026:SCRUTIN:178'
        self.scrutiny.occurred_on = date(2026, 1, 29)
        self.scrutiny.save()
        self.person = Person.objects.create(stable_id='SENAT:PERSON:08012N', display_name='SYNTH Lozach',
                                            institutional_ids={'senat': '08012N'})
        self.vote = VoteObservation.objects.create(scrutiny=self.scrutiny, person=self.person,
            source_document=self.document, source_category='Contre', normalized_category='against', mapping_rule='SYNTH')
        ref = source_ref(self.document)
        self.public = {'public_contract_version': 'dv.content/0.7.0-conclusion-draft.1',
            'personal_vote_selection': {'person_id': self.person.stable_id, 'scrutiny_id': self.scrutiny.stable_id,
                                        'source_version': ref},
            'actors': {'speaker': {'person_id': self.person.stable_id, 'label': self.person.display_name},
                       'represented_entity': {'organization_id': 'SENAT:GROUP:SER'},
                       'compared_actor': {'person_id': None, 'organization_id': 'SENAT:GROUP:SER'}, 'scope': 'collective'},
            'compared_actor_scope': 'collective',
            'qualification_material': {'actors': {}, 'declaration': {'position_scope': 'collective'}},
            'catalogue': {'scope_scrutiny_ids': [self.scrutiny.stable_id], 'vote_observations': [
                {'person_id': self.person.stable_id, 'scrutiny_id': self.scrutiny.stable_id,
                 'source_version': ref, 'normalized_category': 'against'}]}}

    def test_personal_vote_keeps_collective_declaration_and_original_scope_intact(self):
        original = deepcopy(self.public)
        projected, ref = personal_vote_selection_v07(self.public)
        self.assertEqual(self.public, original)
        self.assertEqual(projected['actors']['compared_actor']['person_id'], self.person.stable_id)
        self.assertIsNone(projected['actors']['compared_actor']['organization_id'])
        self.assertEqual(projected['actors']['represented_entity'], {'organization_id': 'SENAT:GROUP:SER'})
        self.assertEqual(projected['qualification_material']['declaration']['position_scope'], 'collective')
        self.assertEqual(projected['compared_actor_scope'], 'personal')
        self.assertEqual(ref, source_ref(self.document))
        self.assertEqual(projected['qualification_material']['actors'], projected['actors'])

    def test_name_proof_flag_wrong_uid_and_wrong_scrutiny_are_rejected(self):
        for changes in ({'name': 'SYNTH Lozach'}, {'proven': True}, {'person_id': 'AN:PERSON:08012N'},
                        {'scrutiny_id': 'SENAT:2025-2026:SCRUTIN:179'}):
            public = deepcopy(self.public)
            public['personal_vote_selection'].update(changes)
            with self.assertRaises(ValidationError):
                personal_vote_selection_v07(public)

    def test_wrong_source_version_sha_url_and_old_profile_are_rejected(self):
        for changes in ({'sha256': 'a' * 64}, {'version_id': 'SYNTH wrong'}, {'public_url': 'https://example.org/SYNTH'}):
            public = deepcopy(self.public)
            public['personal_vote_selection']['source_version'].update(changes)
            with self.assertRaises(ValidationError):
                personal_vote_selection_v07(public)
        public = deepcopy(self.public)
        public['public_contract_version'] = 'dv.content/0.6.0-provenance-draft.1'
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_requires_conclusion_profile'):
            personal_vote_selection_v07(public)

    def test_missing_observation_and_unknown_or_changed_position_are_not_votes(self):
        for category in ('unknown', 'not_recorded', 'for'):
            self.vote.normalized_category = category
            self.vote.save(update_fields=['normalized_category'])
            with self.assertRaises(ValidationError):
                personal_vote_selection_v07(self.public)
        self.vote.delete()
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_observation_missing_or_ambiguous'):
            personal_vote_selection_v07(self.public)

    def test_other_event_date_and_changed_catalogue_position_are_rejected(self):
        self.scrutiny.occurred_on = date(2026, 1, 30)
        self.scrutiny.save(update_fields=['occurred_on'])
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_original_event_position_mismatch'):
            personal_vote_selection_v07(self.public)
        self.scrutiny.occurred_on = date(2026, 1, 29)
        self.scrutiny.save(update_fields=['occurred_on'])
        public = deepcopy(self.public)
        public['catalogue']['vote_observations'][0]['normalized_category'] = 'for'
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_catalogue_observation_mismatch'):
            personal_vote_selection_v07(public)

    def test_changed_original_and_conflicting_group_are_rejected(self):
        self.path.write_text('SYNTH tampered', encoding='utf-8')
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_original_integrity_failed'):
            personal_vote_selection_v07(self.public)
        altered = SENATE.replace('</html>', '<div id="accordion-collapse-scrutin-RDSE"><ul><li><b>Contre</b> : M. SYNTH Lozach</li></ul></div></html>').encode('utf-8')
        self.path.write_bytes(altered)
        self.document.sha256 = hashlib.sha256(altered).hexdigest()
        self.document.byte_size = len(altered)
        self.document.save()
        self.public['personal_vote_selection']['source_version'] = source_ref(self.document)
        with self.assertRaisesMessage(ValidationError, 'personal_vote_selection_original_event_position_mismatch'):
            personal_vote_selection_v07(self.public)
