"""Pure schema/prompt qualification: SYNTH, no Django import, database or provider."""
import ast
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator, ValidationError

SERVICES = Path(__file__).resolve().parents[1] / 'services'
SOURCE = SERVICES / 'verification_counterevidence_executor.py'
AXES = ('contrary_interpretation', 'different_vote_same_object_period', 'affiliation_change',
    'actor_capacity', 'procedural_vote', 'submitted_vs_adopted_text', 'selection_too_narrow', 'quote_out_of_context')
CANON_SPEC = importlib.util.spec_from_file_location('SYNTH_counter_v3_canonical', SERVICES / 'canonical.py')
CANON = importlib.util.module_from_spec(CANON_SPEC)
CANON_SPEC.loader.exec_module(CANON)


def helpers_from_source(source):
    """Execute only pure functions; do not import the service/module or settings."""
    tree = ast.parse(source)
    funcs = {'_object', 'response_schema', '_response_schema_v3', '_prompt'}
    assignments = {'INSTRUCTION', 'INSTRUCTION_V3'}
    nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in funcs)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in assignments for t in node.targets))]
    frame = lambda packet: {'schema': 'SYNTH.transport-only/1', 'packet': deepcopy(packet)}
    wire = SimpleNamespace(build_frame=frame, validate_transport=lambda actual, packet: actual == frame(packet),
        TRANSPORT_INSTRUCTION='SYNTH transport only\n', MAX_WIRE_BYTES=10*1024*1024, MAX_WIRE_CODEPOINTS=10*1024*1024)
    counter = SimpleNamespace(AXES=AXES, ADVERSARIAL_RESPONSE='dv.adversarial-response/1',
        ADVERSARIAL_RESPONSE_V2='dv.adversarial-response/2', ADVERSARIAL_RESPONSE_V3='dv.adversarial-response/3')
    scope = {'counter': counter, 'wire': wire, 'sha256_digest': CANON.sha256_digest,
        'canonical_bytes': CANON.canonical_bytes, 'ExecutionFailure': RuntimeError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope


def synth_packet(profile=3):
    content = {'claims': [
        {'id': 'SYNTH.claim-a', 'support': [
            {'passage_id': 7, 'quote': 'SYNTH exact selected complete support.'},
            {'passage_id': 9, 'quote': 'SYNTH exact second complete support.'}]},
        {'id': 'SYNTH.claim-b', 'support': []}],
        'axes': list(AXES)}
    if profile in (2, 3):
        content['conclusion_contract'] = {'contract': 'SYNTH.conclusion/1'}
        content['conclusion_dependencies'] = {axis: {'required': axis != 'affiliation_change'} for axis in AXES}
    if profile == 3:
        content['response_contract'] = 'dv.adversarial-response/3'
        content['support_references'] = {c['id']: {str(i): CANON.sha256_digest(s) for i, s in enumerate(c['support'])}
            for c in content['claims']}
    return {'fingerprint': '0'*64, 'batch_id': 'adversarial-0000',
        'sources': [{'passages': [{'id': 7, 'exact_text': 'SYNTH exact selected complete support.'},
            {'id': 9, 'exact_text': 'SYNTH exact second complete support.'}]}], 'adversarial': content}


def synth_response(packet):
    content = packet['adversarial']
    return {'contract': 'dv.adversarial-response/3', 'fingerprint': packet['fingerprint'], 'batch_id': packet['batch_id'],
        'claims': {claim['id']: {'conclusion': 'no_objection', 'reason': 'SYNTH transport fixture only.',
            'support_relations': {str(index): {'support_sha256': CANON.sha256_digest(support), 'relation': 'supports',
                'reason': 'SYNTH exact reference only.'} for index, support in enumerate(claim['support'])}}
            for claim in content['claims']},
        'axes': {axis: {'status': 'not_found' if content['conclusion_dependencies'][axis]['required'] else 'not_applicable',
            'reason': 'SYNTH scope; never proof of absence.', 'searched_passage_ids': [7, 9], 'findings': []} for axis in AXES},
        'limitations': ['SYNTH, no real semantic assessment or native call.']}


def synth_finding():
    return {'claim_id': 'SYNTH.claim-a', 'passage_id': 7, 'quote': 'SYNTH exact selected',
        'relation': 'qualifies', 'reason': 'SYNTH schema fixture only.'}


class CounterExecutorV3PureTests(unittest.TestCase):
    def setUp(self):
        self.helper = helpers_from_source(SOURCE.read_text(encoding='utf-8-sig'))
        self.packet = synth_packet()
        self.schema = self.helper['response_schema'](self.packet)
        self.validator = Draft202012Validator(self.schema)
        self.response = synth_response(self.packet)

    def reject(self, response):
        with self.assertRaises(ValidationError):
            self.validator.validate(response)

    def test_valid_compact_response_and_draft_schema(self):
        Draft202012Validator.check_schema(self.schema)
        before = deepcopy(self.packet)
        self.validator.validate(self.response)
        self.assertEqual(self.packet, before)
        self.assertNotIn('quote', self.response['claims']['SYNTH.claim-a']['support_relations']['0'])

    def test_valid_qualified_relation_and_authentic_finding(self):
        self.response['claims']['SYNTH.claim-a']['conclusion'] = 'qualified'
        self.response['claims']['SYNTH.claim-a']['support_relations']['0']['relation'] = 'context_only'
        self.response['axes']['actor_capacity']['status'] = 'found'
        self.response['axes']['actor_capacity']['findings'] = [synth_finding()]
        self.validator.validate(self.response)

    def test_foreign_support_sha_rejected(self):
        self.response['claims']['SYNTH.claim-a']['support_relations']['0']['support_sha256'] = 'f'*64
        self.reject(self.response)

    def test_bool_support_sha_rejected(self):
        self.response['claims']['SYNTH.claim-a']['support_relations']['0']['support_sha256'] = True
        self.reject(self.response)

    def test_missing_or_foreign_support_index_rejected(self):
        for missing in (True, False):
            value = deepcopy(self.response)
            relations = value['claims']['SYNTH.claim-a']['support_relations']
            if missing:
                del relations['1']
            else:
                relations['2'] = deepcopy(relations['1'])
            self.reject(value)

    def test_neighbor_quote_cannot_be_added_or_replace_reference(self):
        self.response['claims']['SYNTH.claim-a']['support_relations']['0']['quote'] = 'SYNTH neighbor quote'
        self.reject(self.response)
        del self.response['claims']['SYNTH.claim-a']['support_relations']['0']['support_sha256']
        self.reject(self.response)

    def test_no_objection_with_unfavorable_relation_rejected(self):
        for relation in ('context_only', 'irrelevant', 'contradicts', 'indeterminate'):
            value = deepcopy(self.response)
            value['claims']['SYNTH.claim-a']['support_relations']['0']['relation'] = relation
            self.reject(value)

    def test_found_without_finding_rejected(self):
        self.response['axes']['actor_capacity']['status'] = 'found'
        self.reject(self.response)

    def test_not_found_or_unavailable_with_finding_rejected(self):
        for status in ('not_found', 'unavailable'):
            value = deepcopy(self.response)
            value['axes']['actor_capacity']['status'] = status
            value['axes']['actor_capacity']['findings'] = [synth_finding()]
            self.reject(value)

    def test_required_axis_not_applicable_rejected(self):
        self.response['axes']['actor_capacity']['status'] = 'not_applicable'
        self.reject(self.response)

    def test_unrequired_axis_not_found_or_nonempty_na_rejected(self):
        self.response['axes']['affiliation_change']['status'] = 'not_found'
        self.reject(self.response)
        self.response['axes']['affiliation_change']['status'] = 'not_applicable'
        self.response['axes']['affiliation_change']['findings'] = [synth_finding()]
        self.reject(self.response)

    def test_exact_passage_scope_missing_extra_duplicate_or_reordered_rejected(self):
        for passages in ([7], [7, 9, 10], [7, 7], [9, 7]):
            value = deepcopy(self.response)
            value['axes']['actor_capacity']['searched_passage_ids'] = passages
            self.reject(value)

    def test_missing_or_foreign_claim_key_rejected(self):
        for missing in (True, False):
            value = deepcopy(self.response)
            if missing:
                del value['claims']['SYNTH.claim-b']
            else:
                value['claims']['SYNTH.foreign'] = deepcopy(value['claims']['SYNTH.claim-b'])
            self.reject(value)

    def test_missing_or_foreign_axis_key_rejected(self):
        for missing in (True, False):
            value = deepcopy(self.response)
            if missing:
                del value['axes']['actor_capacity']
            else:
                value['axes']['SYNTH.foreign'] = deepcopy(value['axes']['actor_capacity'])
            self.reject(value)

    def test_legacy_response_profiles_rejected_by_v3_schema(self):
        for profile in (1, 2):
            value = deepcopy(self.response)
            value['contract'] = 'dv.adversarial-response/' + str(profile)
            self.reject(value)

    def test_v3_is_explicit_and_not_inferred_from_conclusion(self):
        for profile in (1, 2):
            packet = synth_packet(profile)
            schema = self.helper['response_schema'](packet)
            self.assertEqual(schema['properties']['contract']['enum'], ['dv.adversarial-response/' + str(profile)])
            self.assertEqual(schema['properties']['claims']['type'], 'array')
            self.assertNotIn('dv.adversarial-response/3', self.helper['_prompt'](packet))
        prompt = self.helper['_prompt'](self.packet)
        self.assertIn('dv.adversarial-response/3', prompt)
        self.assertIn('support_sha256', prompt)
        self.assertIn('findings vide', prompt)
        self.assertIn('SHA', prompt)

    def test_unknown_explicit_contract_refused(self):
        for explicit in ('dv.adversarial-response/999', None, ''):
            self.packet['adversarial']['response_contract'] = explicit
            with self.assertRaisesRegex(RuntimeError, 'adversarial_response_contract_not_supported'):
                self.helper['response_schema'](self.packet)
            with self.assertRaisesRegex(RuntimeError, 'adversarial_response_contract_not_supported'):
                self.helper['_prompt'](self.packet)

    def test_legacy_schema_and_prompt_golden_digests_unchanged(self):
        golden = {
            1: ('24968ab8eb445c90c61a4380d020104f9db17430a936c17d51f03d7de7b8f59b',
                '08baa0d16f3bb14ac365f891f05409fca10d6aa66d9ed92fdf6ca990749f7752'),
            2: ('45efb051bb45935232ff1a8fd062ad8ec1bf1bd924703efc9cf65a4d08795e8b',
                '3b7d5fad4a84cf0aded562a9200e13ecbf8b0a4501ad507f90d8a3f5eecc3879'),
        }
        for profile, (schema_sha, prompt_sha) in golden.items():
            packet = synth_packet(profile)
            self.assertEqual(CANON.sha256_digest(self.helper['response_schema'](packet)), schema_sha)
            self.assertEqual(hashlib.sha256(self.helper['_prompt'](packet).encode('utf-8')).hexdigest(), prompt_sha)


if __name__ == '__main__':
    unittest.main()
