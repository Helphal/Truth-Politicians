"""Pure SYNTH schema/validator tests: no ORM, provider, settings or native effects.

The local dialect check proves only the deliberately small documented subset;
it does not claim that a provider has accepted the schema.
"""
import ast
from copy import deepcopy
import hashlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError as SchemaError

from atelier.tests.test_counterevidence_executor_v3 import (
    AXES, CANON, SOURCE, synth_finding, synth_packet, synth_response,
)

PROFILE = 'dv.adversarial-schema/4'
DOC = 'https://developers.openai.com/api/docs/guides/structured-outputs'
COUNTER_SOURCE = SOURCE.with_name('verification_counterevidence.py')


def qualify_flat_dialect(schema):
    """Conservative flat-profile subset and public limits, without any request.

    Only closed objects, arrays, primitive string/integer types/enums, and array
    min/maxItems are accepted here. All cross-field semantics stay server-side.
    """
    metrics = {'enum_values': 0, 'properties': 0, 'object_depth': 0, 'string_characters': 0}

    def visit(node, path, depth):
        if not isinstance(node, dict):
            raise ValueError(path + ': schema object required')
        kind = node.get('type')
        allowed = {
            'object': {'type', 'properties', 'required', 'additionalProperties'},
            'array': {'type', 'items', 'minItems', 'maxItems'},
            'string': {'type', 'enum'}, 'integer': {'type', 'enum'},
        }
        if kind not in allowed or set(node) - allowed[kind]:
            raise ValueError(path + ': unsupported flat keyword or type')
        if kind == 'object':
            properties, required = node.get('properties'), node.get('required')
            if (not isinstance(properties, dict) or not isinstance(required, list)
                    or len(required) != len(set(required)) or set(required) != set(properties)
                    or node.get('additionalProperties') is not False):
                raise ValueError(path + ': closed required properties expected')
            depth += 1
            metrics['object_depth'] = max(metrics['object_depth'], depth)
            metrics['properties'] += len(properties)
            for name, value in properties.items():
                if type(name) is not str:
                    raise ValueError(path + ': string property required')
                metrics['string_characters'] += len(name)
                visit(value, path + '/properties/' + name, depth)
        elif kind == 'array':
            for bound in ('minItems', 'maxItems'):
                if bound in node and (type(node[bound]) is not int or node[bound] < 0):
                    raise ValueError(path + ': invalid array bound')
            if node.get('minItems', 0) > node.get('maxItems', float('inf')):
                raise ValueError(path + ': reversed array bounds')
            visit(node.get('items'), path + '/items', depth)
        elif 'enum' in node:
            values = node['enum']
            primitive_type = str if kind == 'string' else int
            if not isinstance(values, list) or not values or any(type(v) is not primitive_type for v in values):
                raise ValueError(path + ': primitive enum required')
            metrics['enum_values'] += len(values)
            text_size = sum(len(v) for v in values if type(v) is str)
            metrics['string_characters'] += text_size
            if len(values) > 250 and text_size > 15000:
                raise ValueError(path + ': individual enum string limit')

    if schema.get('type') != 'object':
        raise ValueError('root object required')
    visit(schema, '$', 0)
    for field, ceiling in (('enum_values', 1000), ('properties', 5000),
                           ('object_depth', 10), ('string_characters', 120000)):
        if metrics[field] > ceiling:
            raise ValueError(field + ': public limit exceeded')
    return metrics


def executor_helpers():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    funcs = {'_object', 'response_schema', '_response_schema_v3', '_response_schema_flat_v4',
             '_prompt', 'prepare_adversarial_plan'}
    assignments = {'INSTRUCTION', 'INSTRUCTION_V3', '_MAX_CLAIMS_UNSET'}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in funcs)
        or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in assignments for t in n.targets))]
    frame = lambda packet: {'schema': 'SYNTH.transport-only/1', 'packet': deepcopy(packet)}
    wire = SimpleNamespace(build_frame=frame, validate_transport=lambda actual, packet: actual == frame(packet),
        TRANSPORT_INSTRUCTION='SYNTH transport only\n', MAX_WIRE_BYTES=10*1024*1024, MAX_WIRE_CODEPOINTS=10*1024*1024)
    counter = SimpleNamespace(AXES=AXES, ADVERSARIAL_RESPONSE='dv.adversarial-response/1',
        ADVERSARIAL_RESPONSE_V2='dv.adversarial-response/2', ADVERSARIAL_RESPONSE_V3='dv.adversarial-response/3',
        SCHEMA_PROFILE_V4=PROFILE)
    scope = {'__package__': 'atelier.services', 'counter': counter, 'wire': wire,
        'sha256_digest': CANON.sha256_digest, 'canonical_bytes': CANON.canonical_bytes,
        'ExecutionFailure': RuntimeError, 'ValidationError': ValueError, 'deepcopy': deepcopy}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope


def strict_counter_helpers():
    """Run the actual pure server validation functions, without service imports."""
    tree = ast.parse(COUNTER_SOURCE.read_text(encoding='utf-8-sig'))
    names = {'_passages', '_schema_profile', '_bounded_reason', '_validate_legacy_adversarial_response',
             '_expand_v3_response', 'validate_adversarial_response', 'expand_adversarial_response'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = {'AXES': AXES, 'SCHEMA_PROFILE_V4': PROFILE, 'ADVERSARIAL_RESPONSE': 'dv.adversarial-response/1',
        'ADVERSARIAL_RESPONSE_V2': 'dv.adversarial-response/2', 'ADVERSARIAL_RESPONSE_V3': 'dv.adversarial-response/3',
        'ValidationError': ValueError, 'deepcopy': deepcopy,
        'sha256_digest': CANON.sha256_digest, 'canonical_bytes': CANON.canonical_bytes}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(COUNTER_SOURCE), 'exec'), scope)
    return scope


def strict_fixture(packet):
    content = packet['adversarial']
    requirements = deepcopy(content['conclusion_dependencies'])
    plan = {'response_contract': 'dv.adversarial-response/3', 'schema_profile': PROFILE,
        'fingerprint': packet['fingerprint'], 'max_output_bytes': 128*1024,
        'snapshot': {'sources': deepcopy(packet['sources']), 'conclusion': {'archetype': 'SYNTH'}},
        'classification': {'dependency_graph': {'counterevidence_requirements': requirements}},
        'first_result': {'claims': deepcopy(content['claims'])}}
    batch = {'batch_id': packet['batch_id'], 'claim_ids': [c['id'] for c in content['claims']],
        'searched_passage_ids': [p['id'] for s in packet['sources'] for p in s['passages']],
        'packet': deepcopy(content)}
    return plan, batch


class CounterSchemaFlatV4Tests(unittest.TestCase):
    def setUp(self):
        self.helper = executor_helpers()
        self.strict = strict_counter_helpers()
        self.packet = synth_packet()
        self.packet['adversarial']['schema_profile'] = PROFILE
        self.schema = self.helper['response_schema'](self.packet)
        self.validator = Draft202012Validator(self.schema)
        self.raw = synth_response(self.packet)
        self.plan, self.batch = strict_fixture(self.packet)

    def schema_rejects(self, value):
        with self.assertRaises(SchemaError):
            self.validator.validate(value)

    def server_rejects(self, value, code):
        with self.assertRaisesRegex(ValueError, code):
            self.strict['validate_adversarial_response'](self.plan, self.batch, value)

    def test_01_valid_flat_raw_draft_and_server_leave_original_unchanged(self):
        Draft202012Validator.check_schema(self.schema)
        before = CANON.canonical_bytes(self.raw)
        self.validator.validate(self.raw)
        result = self.strict['validate_adversarial_response'](self.plan, self.batch, self.raw)
        self.assertIs(result, self.raw)
        self.assertEqual(CANON.canonical_bytes(self.raw), before)

    def test_02_flat_dialect_rejects_conditional_and_compound_enum(self):
        qualify_flat_dialect(self.schema)
        for change in ('anyOf', 'allOf', 'if', 'enum'):
            value = deepcopy(self.schema)
            value['properties']['limitations'][change] = [[]] if change == 'enum' else []
            with self.subTest(keyword=change), self.assertRaisesRegex(ValueError, 'unsupported flat'):
                qualify_flat_dialect(value)

    def test_03_dialect_counts_and_checks_public_limits(self):
        metrics = qualify_flat_dialect(self.schema)
        self.assertLessEqual(metrics['enum_values'], 1000)
        self.assertLessEqual(metrics['object_depth'], 10)
        for field, value in (('enum', ['SYNTH']*1001), ('enum', ['S'*500]*251)):
            schema = {'type': 'object', 'properties': {'x': {'type': 'string', field: value}},
                'required': ['x'], 'additionalProperties': False}
            with self.assertRaisesRegex(ValueError, 'limit'):
                qualify_flat_dialect(schema)

    def test_04_all_nested_objects_are_closed_and_fully_required(self):
        def inspect(value):
            if not isinstance(value, dict):
                return
            if value.get('type') == 'object':
                self.assertIs(value['additionalProperties'], False)
                self.assertEqual(value['required'], list(value['properties']))
                for child in value['properties'].values():
                    inspect(child)
            if value.get('type') == 'array':
                inspect(value['items'])
        inspect(self.schema)
        for target in (self.raw, self.raw['claims']['SYNTH.claim-a'], self.raw['axes']['actor_capacity']):
            target['SYNTH.extra'] = True
            self.schema_rejects(self.raw)
            del target['SYNTH.extra']

    def test_05_exact_claim_keys_required_no_foreign_key(self):
        for missing in (True, False):
            raw = deepcopy(self.raw)
            if missing:
                del raw['claims']['SYNTH.claim-b']
            else:
                raw['claims']['SYNTH.foreign'] = deepcopy(raw['claims']['SYNTH.claim-b'])
            self.schema_rejects(raw)

    def test_06_support_indices_and_sha_are_exact_even_same_quote(self):
        relations = self.raw['claims']['SYNTH.claim-a']['support_relations']
        for value in ('f'*64, True, relations['1']['support_sha256']):
            raw = deepcopy(self.raw)
            raw['claims']['SYNTH.claim-a']['support_relations']['0']['support_sha256'] = value
            self.schema_rejects(raw)
        for key in ('01', '2'):
            raw = deepcopy(self.raw)
            raw['claims']['SYNTH.claim-a']['support_relations'][key] = deepcopy(relations['0'])
            self.schema_rejects(raw)

    def test_07_exact_eight_axes_required_no_foreign_axis(self):
        for missing in (True, False):
            raw = deepcopy(self.raw)
            if missing:
                del raw['axes']['actor_capacity']
            else:
                raw['axes']['SYNTH.foreign'] = deepcopy(raw['axes']['actor_capacity'])
            self.schema_rejects(raw)

    def test_08_pid_items_bounds_foreign_and_bool_rejected_by_schema(self):
        for pids in ([7], [7, 9, 7], [7, 99], [True, 9]):
            raw = deepcopy(self.raw)
            raw['axes']['actor_capacity']['searched_passage_ids'] = pids
            self.schema_rejects(raw)

    def test_09_axis_applicability_still_closed_in_transport(self):
        for axis, status in (('actor_capacity', 'not_applicable'), ('affiliation_change', 'not_found')):
            raw = deepcopy(self.raw)
            raw['axes'][axis]['status'] = status
            self.schema_rejects(raw)
        self.assertEqual(self.schema['properties']['axes']['properties']['actor_capacity']['properties']
            ['searched_passage_ids'], {'type': 'array', 'items': {'type': 'integer', 'enum': [7, 9]},
                'minItems': 2, 'maxItems': 2})

    def test_10_no_objection_unfavorable_relations_remain_server_refused(self):
        for relation in ('context_only', 'irrelevant', 'contradicts', 'indeterminate'):
            raw = deepcopy(self.raw)
            raw['claims']['SYNTH.claim-a']['support_relations']['0']['relation'] = relation
            self.validator.validate(raw)
            self.server_rejects(raw, 'adversarial_unfavorable_relation_cannot_be_promoted')

    def test_11_findings_status_coherence_remains_server_refused(self):
        for status, findings in (('found', []), ('not_found', [synth_finding()]),
                                 ('unavailable', [synth_finding()])):
            raw = deepcopy(self.raw)
            raw['axes']['actor_capacity'].update(status=status, findings=findings)
            self.validator.validate(raw)
            self.server_rejects(raw, 'adversarial_axis_findings_inconsistent')

    def test_12_order_duplicates_and_integer_types_remain_server_refused(self):
        for pids in ([9, 7], [7, 7], [7.0, 9.0]):
            raw = deepcopy(self.raw)
            raw['axes']['actor_capacity']['searched_passage_ids'] = pids
            self.validator.validate(raw)
            self.server_rejects(raw, 'floating_number_forbidden' if type(pids[0]) is float
                else 'adversarial_received_material_scope_not_covered')

    def test_13_text_nonempty_and_bounds_remain_server_refused(self):
        for reason in ('', '   ', 'S'*4001):
            raw = deepcopy(self.raw)
            raw['claims']['SYNTH.claim-a']['reason'] = reason
            self.validator.validate(raw)
            self.server_rejects(raw, 'adversarial_reason_required')

    def test_14_neighbor_cannot_replace_support_and_findings_must_be_authentic(self):
        raw = deepcopy(self.raw)
        raw['claims']['SYNTH.claim-a']['support_relations']['0']['quote'] = 'SYNTH neighbor replacement'
        self.schema_rejects(raw)
        expanded = self.strict['expand_adversarial_response'](self.plan, self.batch, self.raw)
        self.assertEqual(expanded['claims'][0]['support_relations'][0]['quote'],
            self.plan['first_result']['claims'][0]['support'][0]['quote'])
        raw = deepcopy(self.raw)
        finding = synth_finding()
        finding['quote'] = 'SYNTH text absent from original'
        raw['axes']['actor_capacity'].update(status='found', findings=[finding])
        self.validator.validate(raw)
        self.server_rejects(raw, 'adversarial_finding_not_bound_to_received_material')

    def test_15_unknown_profile_or_incompatible_contract_refused(self):
        for profile, contract in ((None, 'dv.adversarial-response/3'), ('', 'dv.adversarial-response/3'),
                ('dv.adversarial-schema/999', 'dv.adversarial-response/3'), (PROFILE, None),
                (PROFILE, 'dv.adversarial-response/2')):
            packet = synth_packet()
            packet['adversarial'].update(schema_profile=profile, response_contract=contract)
            for name in ('response_schema', '_prompt'):
                with self.subTest(profile=profile, contract=contract, helper=name), self.assertRaisesRegex(
                        RuntimeError, 'adversarial_schema_profile_not_supported'):
                    self.helper[name](packet)
        for contract in ('dv.adversarial-response/1', 'dv.adversarial-response/2', 'dv.adversarial-response/4'):
            raw = deepcopy(self.raw)
            raw['contract'] = contract
            self.schema_rejects(raw)

    def test_16_legacy_profiles_and_prompt_bytes_unchanged(self):
        golden = {
            1: ('24968ab8eb445c90c61a4380d020104f9db17430a936c17d51f03d7de7b8f59b',
                '08baa0d16f3bb14ac365f891f05409fca10d6aa66d9ed92fdf6ca990749f7752'),
            2: ('45efb051bb45935232ff1a8fd062ad8ec1bf1bd924703efc9cf65a4d08795e8b',
                '3b7d5fad4a84cf0aded562a9200e13ecbf8b0a4501ad507f90d8a3f5eecc3879'),
            3: ('9ca22f30eff9306c629debb24aed967670a4ff10336530caeb21d19a1faba061',
                'eba327ffb442a2b346e1fe0fa4d2d0e9b8b7d820346ac2b90d8835a3f3359a8b'),
        }
        for profile, (schema_sha, prompt_sha) in golden.items():
            packet = synth_packet(profile)
            self.assertEqual(CANON.sha256_digest(self.helper['response_schema'](packet)), schema_sha)
            self.assertEqual(hashlib.sha256(self.helper['_prompt'](packet).encode()).hexdigest(), prompt_sha)
        prompt = self.helper['_prompt'](self.packet)
        self.assertTrue(prompt.startswith(self.helper['INSTRUCTION_V3']))
        self.assertNotEqual(CANON.sha256_digest(self.schema), golden[3][0])

    def test_17_prepare_omitted_default_and_explicit_values_forwarded(self):
        calls = []
        user = SimpleNamespace(pk=3)
        revision = SimpleNamespace(pk=5, created_by_id=1)
        first = SimpleNamespace(pk=11, job_id=9, job=SimpleNamespace(key='SYNTH-job', input_snapshot={}),
            attempt=SimpleNamespace(actor_id=2, result={}))
        queryset = lambda value: SimpleNamespace(objects=SimpleNamespace(
            select_related=lambda *args: SimpleNamespace(get=lambda **kwargs: value)))
        self.helper.update(EditorialRevision=queryset(revision), VerificationReceipt=queryset(first),
            v=SimpleNamespace(require_verifier=lambda value: None), _runtime=lambda *args: None,
            layered=SimpleNamespace(semantic_result=lambda *args: {'SYNTH': 'first'}))
        self.helper['counter'].build_adversarial_plan = lambda *args, **kwargs: calls.append(kwargs) or {'batches': []}
        stub = ModuleType('atelier.services.proof_architecture')
        stub.received_plan = lambda *args: {'proof_architecture': {'classification': {}}}
        for kwargs in ({}, {'response_contract': 'dv.adversarial-response/3', 'schema_profile': PROFILE},
                       {'max_claims': 8}, {'max_claims': 10}, {'max_claims': None}):
            with patch.dict('sys.modules', {stub.__name__: stub}), self.assertRaisesRegex(
                    ValueError, 'adversarial_no_semantic_claims'):
                self.helper['prepare_adversarial_plan'](5, user=user, first_receipt_id=11, **kwargs)
            forwarded = calls[-1]
            for key in ('max_claims', 'schema_profile', 'response_contract'):
                self.assertEqual(key in forwarded, key in kwargs)
                if key in kwargs:
                    self.assertEqual(forwarded[key], kwargs[key])

    def test_18_native_validation_requires_matching_accepted_phase_and_original_return(self):
        # AST serializations differ across Python minors and do not prove that
        # a native receipt is authentic. Exercise the actual pure boundary.
        tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_native_validator')
        state = {'phase_id': 'SYNTH:phase', 'state': 'accepted', 'run_number': 1}
        plan = {'batches': [{'batch_id': 'SYNTH-batch'}]}
        ledger = {'phases': [deepcopy(state)]}
        journal = {'batches': {'SYNTH-batch': deepcopy(state)}}
        original = {'SYNTH': 'native-return'}
        responses = {'SYNTH-batch': deepcopy(original)}
        calls = []

        def validate_return(*args):
            calls.append(args)
            return deepcopy(original)

        scope = {'shared': SimpleNamespace(_allocation_snapshot=lambda _: ledger),
                 '_validate_return': validate_return, '_directory': lambda _: Path('SYNTH')}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), scope)
        validate = scope['_native_validator']
        self.assertTrue(validate(plan, journal, 'SYNTH-allocation', responses))
        self.assertEqual(len(calls), 1)
        responses['SYNTH-batch'] = {'SYNTH': 'forged-return'}
        self.assertFalse(validate(plan, journal, 'SYNTH-allocation', responses))
        responses['SYNTH-batch'] = deepcopy(original)
        for altered in (None, {**state, 'state': 'pending'}, {**state, 'run_number': 2}):
            calls.clear()
            journal['batches']['SYNTH-batch'] = altered
            self.assertFalse(validate(plan, journal, 'SYNTH-allocation', responses))
            self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
