"""Pointers may be structural; their actors, dates and conclusions never are."""
from copy import deepcopy

from django.test import SimpleTestCase

from atelier.services.verification import structural_metadata


class ReceivedPointerMetadataTests(SimpleTestCase):
    def setUp(self):
        self.ref = {'source_id': 'SYNTH-SOURCE', 'version_id': 'r1',
                    'sha256': 'a' * 64, 'public_url': 'https://example.org/primary'}
        self.quote = 'SYNTH déclaration exacte.'
        self.payload = {
            'declaration_quote': self.quote,
            'evidence': [{'source_version': deepcopy(self.ref), 'locator': 'ligne 1',
                          'exact_quote': self.quote, 'transcription_status': 'source_text'}],
            'acts': [{'scrutiny_id': 'SYNTH-VOTE', 'source_version': deepcopy(self.ref),
                      'official_vote_url': self.ref['public_url'],
                      'official_text_url': self.ref['public_url']}],
            'catalogue': {
                'scope_scrutiny_ids': ['SYNTH-VOTE'],
                'legislative_dossiers': [{'id': 'SYNTH-DOSSIER'}],
                'text_versions': [{'id': 'SYNTH-TEXT', 'dossier_id': 'SYNTH-DOSSIER',
                                   'relation': 'submitted', 'official_url': self.ref['public_url'],
                                   'source_version': deepcopy(self.ref)}],
                'vote_observations': [{'mapping_rule': 'official_senate_nominal_and_group_views_20260930',
                                       'source_version': deepcopy(self.ref)}]},
            'qualification_material': {
                'declaration_source': {'public_url': self.ref['public_url'], 'document_version': 'r1',
                                       'sha256': 'a' * 64, 'locator': 'ligne 1'},
                'collective_vote_distribution': {
                    'rule_id': 'COLLECTIVE_AFFILIATION_DATED_V1', 'rule_version': 'racc001-1',
                    'voices': [{'person_id': 'SYNTH-ACTOR', 'sources': [{
                        'source_id': 'SYNTH-SOURCE', 'version_id': 'r1', 'sha256': 'a' * 64,
                        'category': 'for'}]}]}}}
        self.snapshot = {'content': {'public_payload': self.payload, 'source_snapshot': [self.ref]},
                         'sources': [{'reference': deepcopy(self.ref), 'passages': [{
                             'locator': 'ligne 1', 'exact_text': self.quote,
                             'transcription_status': 'source_text'}]}]}

    def allowed(self, suffix, value):
        return structural_metadata({'location': '/public_payload/' + suffix, 'value': value}, self.snapshot)

    def test_text_pointer_is_unique_and_source_bound(self):
        self.assertTrue(self.allowed('catalogue/text_versions/0/id','SYNTH-TEXT'))
        self.payload['catalogue']['text_versions'].append(deepcopy(self.payload['catalogue']['text_versions'][0]))
        self.assertFalse(self.allowed('catalogue/text_versions/0/id','SYNTH-TEXT'))
        self.payload['catalogue']['text_versions'].pop()
        self.payload['catalogue']['text_versions'][0]['source_version']['sha256']='b'*64
        self.assertFalse(self.allowed('catalogue/text_versions/0/id','SYNTH-TEXT'))

    def test_declaration_local_key_does_not_remove_source_requirement(self):
        self.payload['qualification_material']['declaration_id']='DECL:SYNTH:1'
        self.assertTrue(self.allowed('qualification_material/declaration_id','DECL:SYNTH:1'))
        self.payload['qualification_material']['declaration_source']['locator']='unknown'
        self.assertFalse(self.allowed('qualification_material/declaration_id','DECL:SYNTH:1'))

    def test_theme_exemption_requires_explicit_candidate_marker(self):
        self.payload['themes']=['THEME:SYNTH']
        self.assertFalse(self.allowed('themes/0','THEME:SYNTH'))
        self.payload['method_explanation']={'candidate_only':True}
        self.assertTrue(self.allowed('themes/0','THEME:SYNTH'))
        self.payload['method_explanation']['candidate_only']='true'
        self.assertFalse(self.allowed('themes/0','THEME:SYNTH'))

    def test_evidence_locator_requires_received_tuple_quote_and_exact_locator(self):
        path = 'evidence/0/locator'
        self.assertTrue(self.allowed(path, 'ligne 1'))
        self.payload['evidence'][0]['exact_quote'] = 'SYNTH texte non reçu'
        self.assertFalse(self.allowed(path, 'ligne 1'))
        self.payload['evidence'][0]['exact_quote'] = self.quote
        self.payload['evidence'][0]['locator'] = 'ligne inventée'
        self.assertFalse(self.allowed(path, 'ligne inventée'))

    def test_transcription_status_is_copied_from_the_same_passage(self):
        self.assertTrue(self.allowed('evidence/0/transcription_status', 'source_text'))
        self.payload['evidence'][0]['transcription_status'] = 'verified_translation'
        self.assertFalse(self.allowed('evidence/0/transcription_status', 'verified_translation'))

    def test_declaration_reference_is_bound_to_quote_and_passage(self):
        root = 'qualification_material/declaration_source/'
        for name, value in self.payload['qualification_material']['declaration_source'].items():
            self.assertTrue(self.allowed(root + name, value))
        self.payload['declaration_quote'] = 'SYNTH citation étrangère'
        self.assertFalse(self.allowed(root + 'sha256', 'a' * 64))

    def test_voice_source_metadata_does_not_exempt_voice_identity_or_category(self):
        root = 'qualification_material/collective_vote_distribution/voices/0/'
        self.assertTrue(self.allowed(root + 'sources/0/sha256', 'a' * 64))
        for path, value in [('person_id', 'SYNTH-ACTOR'), ('sources/0/category', 'for')]:
            self.assertFalse(self.allowed(root + path, value))
        source = self.payload['qualification_material']['collective_vote_distribution']['voices'][0]['sources'][0]
        source['version_id'] = 'foreign'
        self.assertFalse(self.allowed(root + 'sources/0/sha256', 'a' * 64))

    def test_rule_names_are_closed_and_not_arbitrary_text(self):
        root = 'qualification_material/collective_vote_distribution/'
        self.assertTrue(self.allowed(root + 'rule_version', 'racc001-1'))
        self.payload['qualification_material']['collective_vote_distribution']['rule_id'] = 'EXONERATE_ALL_ACTORS'
        self.assertFalse(self.allowed(root + 'rule_version', 'racc001-1'))
        self.assertTrue(self.allowed('catalogue/vote_observations/0/mapping_rule',
                                     'official_senate_nominal_and_group_views_20260930'))
        self.payload['catalogue']['vote_observations'][0]['mapping_rule'] = 'arbitrary_new_rule'
        self.assertFalse(self.allowed('catalogue/vote_observations/0/mapping_rule', 'arbitrary_new_rule'))

    def test_vote_url_cannot_be_replaced_with_another_received_source(self):
        other = {**self.ref, 'source_id': 'SYNTH-OTHER', 'public_url': 'https://example.org/other'}
        self.snapshot['content']['source_snapshot'].append(other)
        self.assertTrue(self.allowed('acts/0/official_vote_url', self.ref['public_url']))
        self.payload['acts'][0]['official_vote_url'] = other['public_url']
        self.assertFalse(self.allowed('acts/0/official_vote_url', other['public_url']))

    def test_text_url_requires_unambiguous_submitted_text_in_container(self):
        self.assertTrue(self.allowed('acts/0/official_text_url', self.ref['public_url']))
        self.payload['catalogue']['text_versions'].append(deepcopy(self.payload['catalogue']['text_versions'][0]))
        self.assertFalse(self.allowed('acts/0/official_text_url', self.ref['public_url']))

    def test_dossier_pointer_requires_one_target_and_existing_reverse_link(self):
        self.assertTrue(self.allowed('catalogue/legislative_dossiers/0/id', 'SYNTH-DOSSIER'))
        self.assertTrue(self.allowed('catalogue/text_versions/0/dossier_id', 'SYNTH-DOSSIER'))
        # La clé de texte est désormais un pointeur local reçu ; sa source et
        # son unicité ont leurs contre-tests, indépendants du lien de dossier.
        self.assertTrue(self.allowed('catalogue/text_versions/0/id', 'SYNTH-TEXT'))
        self.payload['catalogue']['text_versions'][0]['dossier_id'] = 'UNRELATED'
        self.assertFalse(self.allowed('catalogue/legislative_dossiers/0/id', 'SYNTH-DOSSIER'))

    def test_semantics_never_become_metadata_through_a_known_prefix(self):
        for path, value in [
            ('acts/0/included', True), ('attribution/person_id', 'SYNTH-ACTOR'),
            ('catalogue/scrutinies/0/counts/registered/state', 'unknown'),
            ('catalogue/affiliations/0/starts_on', '2026-01-29'),
            ('counterevidence/0/statement', 'SYNTH contradiction'),
            ('themes/0', 'THEME:LOCAL-PROPOSAL'),
            ('qualification_material/collective_vote_distribution/usable_voices', 65),
        ]:
            self.assertFalse(self.allowed(path, value), path)

    def test_missing_or_corrupted_pointer_structure_is_refused(self):
        self.assertFalse(self.allowed('evidence/99/locator', 'ligne 1'))
        self.payload['evidence'][0]['source_version']['sha256'] = 'b' * 64
        self.assertFalse(self.allowed('evidence/0/locator', 'ligne 1'))

    def test_local_text_identifier_requires_unique_received_source_binding(self):
        path = 'catalogue/text_versions/0/id'
        self.assertTrue(self.allowed(path, 'SYNTH-TEXT'))
        text = self.payload['catalogue']['text_versions'][0]
        text['source_version']['version_id'] = 'unreceived'
        self.assertFalse(self.allowed(path, 'SYNTH-TEXT'))
        text['source_version'] = deepcopy(self.ref)
        self.payload['catalogue']['text_versions'].append(deepcopy(text))
        self.assertFalse(self.allowed(path, 'SYNTH-TEXT'))

    def test_local_declaration_identifier_does_not_bypass_quote_or_namespace(self):
        material = self.payload['qualification_material']
        material['declaration_id'] = 'DECL:SYNTH:1'
        path = 'qualification_material/declaration_id'
        self.assertTrue(self.allowed(path, 'DECL:SYNTH:1'))
        material['declaration_source']['locator'] = 'unreceived locator'
        self.assertFalse(self.allowed(path, 'DECL:SYNTH:1'))
        material['declaration_source']['locator'] = 'ligne 1'
        material['declaration_id'] = 'PERSON:SYNTH:1'
        self.assertFalse(self.allowed(path, 'PERSON:SYNTH:1'))

    def test_taxonomy_requires_literal_candidate_flag_and_unique_bounded_codes(self):
        self.payload['themes'] = ['THEME:LOCAL']
        self.payload['method_explanation'] = {'candidate_only': True}
        self.assertTrue(self.allowed('themes/0', 'THEME:LOCAL'))
        for flag in (1, 'true', False, None):
            self.payload['method_explanation']['candidate_only'] = flag
            self.assertFalse(self.allowed('themes/0', 'THEME:LOCAL'))
        self.payload['method_explanation']['candidate_only'] = True
        self.payload['themes'].append('THEME:LOCAL')
        self.assertFalse(self.allowed('themes/0', 'THEME:LOCAL'))
        self.payload['themes'] = ['THEME:' + 'x' * 101]
        self.assertFalse(self.allowed('themes/0', self.payload['themes'][0]))

    def test_candidate_taxonomy_never_exempts_actor_counterevidence_or_votes(self):
        self.payload['method_explanation'] = {'candidate_only': True}
        self.payload['attribution'] = {'person_id': 'DECL:SYNTH:1'}
        self.payload['counterevidence'] = [{'statement': 'THEME:COUNTEREVIDENCE'}]
        self.payload['acts'][0]['included'] = True
        for path, value in [('attribution/person_id', 'DECL:SYNTH:1'),
                            ('counterevidence/0/statement', 'THEME:COUNTEREVIDENCE'),
                            ('acts/0/included', True)]:
            self.assertFalse(self.allowed(path, value))
