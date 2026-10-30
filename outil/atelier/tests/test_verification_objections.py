"""Service intégré pur ; fixtures SYNTH, aucune base ou inférence."""
from copy import deepcopy
import hashlib
from unittest import TestCase

from atelier.services.canonical import sha256_digest
from atelier.services.verification_objections import (
    ObjectionContractError, build_objection_queue, build_review_artifact,
    prepare_versioned_proposal, prepare_compatible_documentary_candidate, resolve_pointer,
)


class ObjectionQueueTests(TestCase):
    def setUp(self):
        self.original = '<a href="/senateur/synth12345a.html">SYNTH Acteur</a>'
        digest = hashlib.sha256(self.original.encode('utf-8')).hexdigest()
        self.ref = {'source_id': 'SYNTH', 'version_id': 'r1', 'sha256': digest,
                    'public_url': 'https://example.invalid/source'}
        self.snapshot = {'object': 'SYNTH-OBJECT', 'revision': 2, 'policy': 'dv.verification/1.1.0',
            'content': {'title': 'SYNTH', 'result': 'convergence', 'main_caveat': 'SYNTH réserve existante',
                'public_payload': {'actors': {'speaker': {'person_id': 'SYNTH:PERSON:12345A'}},
                    'scope': 'SYNTH ponctuel', 'flag': True, 'a/b~c': 'SYNTH valeur'}},
            'sources': [{'reference': self.ref, 'passages': [{'id': 1, 'locator': 'SYNTH ligne 1',
                'exact_text': 'SYNTH déclaration littérale.', 'public_url': self.ref['public_url']}]}]}
        self.claims = [
            {'id': 'a', 'location': '/title', 'value': 'SYNTH'},
            {'id': 'b', 'location': '/result', 'value': 'convergence'},
            {'id': 'c', 'location': '/public_payload/actors/speaker/person_id', 'value': 'SYNTH:PERSON:12345A'},
            {'id': 'd', 'location': '/public_payload/flag', 'value': True},
        ]
        self.snapshot['claims'] = self.claims
        self.binding = {'job_id': 1, 'revision_pk': 2, 'revision': 2, 'attempt_id': 3,
            'object': 'SYNTH-OBJECT', 'fingerprint': sha256_digest(self.snapshot), 'raw_sha256': 'a' * 64}
        self.raw = {'contract': 'dv.compact-claims/1', 'fingerprint': self.binding['fingerprint'],
            'groups': [{'claim_indices': [0], 'verdict': 'ETAYE_DANS_PERIMETRE', 'reason': 'SYNTH',
                        'nature': 'fact', 'action': 'conserver', 'support': [], 'against': [], 'limits': []},
                       {'claim_indices': [1, 2], 'verdict': 'PARTIEL', 'reason': 'SYNTH objection',
                        'nature': 'interpretation', 'action': 'nuancer',
                        'support': [{'passage_id': 1, 'quote': 'SYNTH déclaration'}],
                        'against': [], 'limits': ['SYNTH limite']}], 'limitations': []}
        self.semantic = deepcopy(self.claims[:3])

    def build(self, **kwargs):
        return build_objection_queue(self.snapshot, self.raw, self.binding, model_claims=self.semantic, **kwargs)

    def assessment(self, **changes):
        value = {'code': 'refuted_with_evidence', 'rationale': 'SYNTH contrôle indépendant',
            'required_action': 'SYNTH conserver la réserve', 'case_ids': ['C3.4'],
            'evidence_families': ['derived_relation'], 'raw_sha256': self.binding['raw_sha256'],
            'fingerprint': self.binding['fingerprint'], 'group_sha256': sha256_digest(self.raw['groups'][1]),
            'proofs': [{'kind': 'snapshot_pointer', 'pointer': '/main_caveat', 'expected': 'SYNTH réserve existante'}]}
        value.update(changes)
        return {'1:1': value}

    def refuses(self, code, function=None):
        with self.assertRaisesRegex(ObjectionContractError, '^' + code + '$'):
            (function or self.build)()

    def test_raw_and_snapshot_remain_exact(self):
        before = deepcopy((self.snapshot, self.raw, self.binding, self.semantic))
        queue = self.build()
        self.assertEqual((self.snapshot, self.raw, self.binding, self.semantic), before)
        self.assertEqual(queue['objections'][0]['model_group'], self.raw['groups'][1])

    def test_effective_indices_map_to_full_snapshot(self):
        self.semantic = [self.claims[2], self.claims[0], self.claims[1]]
        queue = self.build()
        self.assertEqual([c['id'] for c in queue['objections'][0]['claims']], ['a', 'b'])

    def test_complete_unfavorable_is_never_receipt_or_eligible(self):
        queue = self.build(assessments=self.assessment())
        self.assertEqual(queue['coverage']['missing_count'], 0)
        self.assertEqual(queue['axes']['documentary'], 'objections_instructed')
        self.assertEqual(queue['axes']['diffusion'], 'not_eligible')
        self.assertFalse(queue['is_verification_receipt'])
        self.assertFalse(queue['model_judgments_changed'])

    def test_unassessed_is_pending_not_error_confirmed(self):
        self.assertEqual(self.build()['objections'][0]['qualification']['code'], 'pending')

    def test_missing_and_duplicate_coverage_are_exposed(self):
        self.raw['groups'][1]['claim_indices'] = [1, 1]
        coverage = self.build()['coverage']
        self.assertEqual((coverage['present_count'], coverage['missing_count'], coverage['duplicate_count']), (2, 1, 1))
        self.assertEqual(coverage['missing_indices'], [2])

    def test_foreign_fingerprint_refused(self):
        self.raw['fingerprint'] = 'b' * 64
        self.refuses('snapshot_or_raw_fingerprint_mismatch')

    def test_foreign_revision_refused(self):
        self.binding['revision'] = 3
        self.refuses('revision_or_object_mismatch')

    def test_changed_source_snapshot_refused(self):
        self.snapshot['sources'][0]['reference']['version_id'] = 'r2'
        self.refuses('snapshot_or_raw_fingerprint_mismatch')

    def test_changed_mapping_refused(self):
        self.semantic[1]['value'] = 'SYNTH invented'
        self.refuses('model_claim_mapping_changed')

    def test_boolean_is_not_integer(self):
        self.semantic = deepcopy(self.claims)
        self.semantic[3]['value'] = 1
        self.refuses('model_claim_mapping_changed')

    def test_duplicate_identity_refused(self):
        self.semantic.append(deepcopy(self.semantic[0]))
        self.refuses('model_claim_identity_ambiguous')

    def test_out_of_range_index_refused(self):
        self.raw['groups'][1]['claim_indices'].append(3)
        self.refuses('model_claim_index_out_of_range')

    def test_boolean_index_refused(self):
        self.raw['groups'][1]['claim_indices'][0] = True
        self.refuses('model_claim_index_out_of_range')

    def test_context_is_actual_parent_and_caveat(self):
        queue = self.build()
        person = queue['objections'][0]['claims'][1]
        self.assertEqual(person['context'], {'person_id': 'SYNTH:PERSON:12345A'})
        self.assertEqual(queue['context']['main_caveat'], 'SYNTH réserve existante')

    def test_bad_model_quote_flagged_without_repair(self):
        self.raw['groups'][1]['support'][0]['quote'] = 'SYNTH autre citation'
        queue = self.build()
        self.assertFalse(queue['objections'][0]['source_references'][0]['quote_literal'])
        self.assertEqual(queue['objections'][0]['model_group']['support'][0]['quote'], 'SYNTH autre citation')

    def test_model_reference_foreign_passage_is_exposed(self):
        self.raw['groups'][1]['support'][0]['passage_id'] = 999
        self.assertFalse(self.build()['objections'][0]['source_references'][0]['quote_literal'])

    def test_qualification_does_not_change_model_verdict(self):
        queue = self.build(assessments=self.assessment())
        self.assertEqual(queue['objections'][0]['model_group']['verdict'], 'PARTIEL')
        self.assertEqual(queue['objections'][0]['qualification']['label'], 'objection réfutée avec preuve')

    def test_no_positive_whitelist_qualification(self):
        self.refuses('assessment_qualification_unknown', lambda: self.build(assessments=self.assessment(code='approved')))

    def test_stale_assessment_raw_refused(self):
        self.refuses('assessment_binding_changed', lambda: self.build(assessments=self.assessment(raw_sha256='b' * 64)))

    def test_assessment_for_unknown_group_refused(self):
        a = self.assessment(); a['1:999'] = a.pop('1:1')
        self.refuses('assessment_group_not_present', lambda: self.build(assessments=a))

    def test_assessment_requires_actual_proof(self):
        self.refuses('assessment_proof_required', lambda: self.build(assessments=self.assessment(proofs=[])))

    def test_proof_actual_value_checked(self):
        proof = [{'kind': 'snapshot_pointer', 'pointer': '/main_caveat', 'expected': 'SYNTH absence de réserve'}]
        self.refuses('assessment_snapshot_proof_mismatch', lambda: self.build(assessments=self.assessment(proofs=proof)))

    def test_source_proof_requires_literal_quote(self):
        proof = [{'kind': 'source_passage', 'passage_id': 1, 'quote': 'SYNTH paraphrase'}]
        self.refuses('assessment_quote_not_literal', lambda: self.build(assessments=self.assessment(proofs=proof)))

    def test_exact_original_segment_proof(self):
        proof = [{'kind': 'pinned_original_segment', 'original_sha256': self.ref['sha256'],
                  'source_version': self.ref, 'start': 0, 'end': len(self.original), 'exact_segment': self.original}]
        q = self.build(assessments=self.assessment(proofs=proof),
                       source_originals={self.ref['sha256']: self.original.encode('utf-8')})
        self.assertEqual(q['objections'][0]['qualification']['proofs'][0]['verification'], 'exact_segment_in_pinned_original')

    def test_changed_original_hash_refused(self):
        proof = [{'kind': 'pinned_original_segment', 'original_sha256': self.ref['sha256'],
                  'source_version': self.ref, 'start': 0, 'end': len(self.original), 'exact_segment': self.original}]
        self.refuses('assessment_original_hash_mismatch', lambda: self.build(assessments=self.assessment(proofs=proof),
            source_originals={self.ref['sha256']: b'SYNTH changed'}))

    def test_exact_original_offset_no_fuzzy_repair(self):
        proof = [{'kind': 'pinned_original_segment', 'original_sha256': self.ref['sha256'],
                  'source_version': self.ref, 'start': 1, 'end': len(self.original), 'exact_segment': self.original}]
        self.refuses('assessment_original_segment_mismatch', lambda: self.build(assessments=self.assessment(proofs=proof),
            source_originals={self.ref['sha256']: self.original.encode('utf-8')}))

    def test_bound_artifact_does_not_claim_readback(self):
        proof = [{'kind': 'bound_artifact', 'path': 'SYNTH.json', 'sha256': 'a' * 64, 'observation': 'SYNTH état'}]
        q = self.build(assessments=self.assessment(proofs=proof))
        self.assertEqual(q['objections'][0]['qualification']['proofs'][0]['verification'], 'external_readback_required')

    def test_pointer_escape_supported(self):
        self.assertEqual(resolve_pointer(self.snapshot['content'], '/public_payload/a~1b~0c'), 'SYNTH valeur')

    def test_invalid_pointer_escape_refused(self):
        self.refuses('invalid_pointer_escape', lambda: resolve_pointer({}, '/a~2b'))

    def test_negative_array_index_refused(self):
        self.refuses('invalid_array_index', lambda: resolve_pointer([1, 2], '/-1'))

    def test_review_artifact_exact_binding_and_false_gates(self):
        artifact = build_review_artifact(self.snapshot, self.raw, self.binding, model_claims=self.semantic)
        self.assertEqual(artifact['revision_pk'], 2)
        self.assertEqual(artifact['raw_file'], 'response.json')
        self.assertEqual(artifact['expected_count'], 3)
        self.assertFalse(artifact['human_review'])
        self.assertFalse(artifact['publication'])

    def test_versioned_proposal_does_not_mutate_historical_snapshot(self):
        before = deepcopy(self.snapshot)
        p = prepare_versioned_proposal(self.snapshot, self.binding,
            [{'location': '/title', 'before': 'SYNTH', 'after': 'SYNTH rôle corrigé'}],
            proposal_id='SYNTH-P1', rationale='SYNTH raison', proofs=[{'kind': 'SYNTH'}])
        self.assertEqual(self.snapshot, before)
        self.assertEqual(p['target_revision'], 3)
        self.assertEqual(p['status'], 'proposed_not_applied')
        self.assertFalse(p['verification_receipt'])

    def test_versioned_proposal_refuses_changed_before(self):
        self.refuses('proposal_before_changed', lambda: prepare_versioned_proposal(self.snapshot, self.binding,
            [{'location': '/title', 'before': 'SYNTH autre', 'after': 'SYNTH corrigé'}],
            proposal_id='SYNTH-P1', rationale='SYNTH raison', proofs=[{'kind': 'SYNTH'}]))

    def test_compatible_candidate_minimal_string_and_protected_context(self):
        self.snapshot['content']['public_payload']['method_explanation'] = {'scope':'SYNTH ancien'}
        self.binding['fingerprint'] = sha256_digest(self.snapshot)
        before = deepcopy(self.snapshot)
        result = prepare_compatible_documentary_candidate(self.snapshot,self.binding,
            [{'location':'/public_payload/method_explanation/scope','before':'SYNTH ancien','after':'SYNTH rôle explicite'}],
            proposal_id='SYNTH-compatible',rationale='SYNTH précision',proofs=[{'kind':'SYNTH'}])
        self.assertEqual(self.snapshot,before)
        self.assertEqual(result['candidate_content']['result'],'convergence')
        self.assertEqual(result['candidate_content']['main_caveat'],'SYNTH réserve existante')
        self.assertFalse(result['proposal']['contract_evolution_required'])

    def test_compatible_candidate_cannot_change_result(self):
        self.refuses('compatible_candidate_change_not_allowed',lambda: prepare_compatible_documentary_candidate(
            self.snapshot,self.binding,[{'location':'/result','before':'convergence','after':'divergence'}],
            proposal_id='SYNTH',rationale='SYNTH',proofs=[{'kind':'SYNTH'}]))

    def test_compatible_candidate_cannot_change_sources_or_affiliations(self):
        for location in ('/source_snapshot/0/sha256','/public_payload/catalogue/affiliations/0/starts_on'):
            self.refuses('compatible_candidate_change_not_allowed',lambda: prepare_compatible_documentary_candidate(
                self.snapshot,self.binding,[{'location':location,'before':'SYNTH','after':'SYNTH autre'}],
                proposal_id='SYNTH',rationale='SYNTH',proofs=[{'kind':'SYNTH'}]))

    def test_compatible_candidate_cannot_extend_contract(self):
        self.refuses('compatible_candidate_change_not_allowed',lambda: prepare_compatible_documentary_candidate(
            self.snapshot,self.binding,[{'location':'/public_payload/method_explanation/result_scope','before':'SYNTH','after':{'code':'SYNTH'}}],
            proposal_id='SYNTH',rationale='SYNTH',proofs=[{'kind':'SYNTH'}]))
