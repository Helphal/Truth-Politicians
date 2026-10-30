"""Tiny SYNTH lossless transport fixtures. No subprocess/provider/database."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.services import verification_protocol as p
from atelier.services import verification_transport as t


def text_sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def packet_fixture(text=None):
    # Distinct NFD/NFC, astral codepoint, whitespace, NUL and literal ref-shaped prose.
    exact = text or ('SYNTH e\u0301/é 🛰️\n  exact\t\x00 {"$dv_text_ref":"prose"}\n' * 9)
    contrary = 'SYNTH contre-élément contraire à la proposition.\n' * 5
    packet = {'protocol': p.PROTOCOL, 'contract': p.v.POLICY,
              'fingerprint': text_sha('SYNTH current snapshot'), 'batch_id': 'claims-0000',
              'offset_unit': 'unicode_codepoints', 'sources': [], 'segments': []}
    for source_number, passage in enumerate((exact, contrary), 1):
        ref = {'source_id': 'SYNTH-source-' + str(source_number), 'version_id': 'SYNTH-version-1',
               'sha256': text_sha('SYNTH original-' + str(source_number))}
        packet['sources'].append({'reference': ref, 'purpose': 'SYNTH support' if source_number == 1 else 'SYNTH against',
            'passages': [{'id': source_number, 'exact_text': passage,
                          'locator': {'page': source_number, 'label': 'SYNTH exact'}, 'verbatim': True}]})
        middle = max(1, len(passage) // 2)
        for start, end in ((0, middle), (middle, len(passage))):
            fields = {'source_id': ref['source_id'], 'version_id': ref['version_id'],
                      'source_sha256': ref['sha256'], 'passage_id': source_number,
                      'passage_sha256': text_sha(passage), 'start': start, 'end': end,
                      'offset_unit': 'unicode_codepoints', 'text': passage[start:end],
                      'text_sha256': text_sha(passage[start:end]), 'locator': {'page': source_number}}
            fields['segment_id'] = 'seg-' + sha256_digest([packet['protocol'], packet['fingerprint'], fields])
            packet['segments'].append(fields)
    repeated = 'SYNTH contexte intégral et réserve explicite. ' * 11
    context = {'object': {'text': repeated, 'reserve': repeated, 'active': True, 'count': 1, 'missing': None},
               'ancestors': [{'type': 'SYNTH actor', 'text': repeated}]}
    packet['assertions'] = [{'assertion_id': 'assert-' + text_sha('SYNTH ' + str(number)),
                            'context': deepcopy(context), 'value': repeated,
                            'location': '/SYNTH/' + str(number), 'structural_metadata': False}
                           for number in range(3)]
    packet['global_context'] = {'main_caveat': repeated, 'title': 'SYNTH exact original title'}
    packet['documentary_evidence'] = {'contract': p.DOCUMENTARY_INPUT, 'fingerprint': packet['fingerprint'],
        'records': [{'record_id': 'SYNTH-record-1', 'family': 'source_status', 'purpose': repeated,
                     'proof': {'kind': 'snapshot_pointer', 'pointer': '/SYNTH', 'expected': repeated},
                     'control': 'SYNTH context requires verifier judgment'}]}
    packet['evidence_findings'] = [{'source_id': packet['sources'][1]['reference']['source_id'],
                                  'finding': repeated, 'limits': [repeated],
                                  'segment_ids': [packet['segments'][-1]['segment_id']]}]
    return packet


class LosslessTransportTests(unittest.TestCase):
    def assert_refused(self, callable_, code=None):
        with self.assertRaises(t.TransportFailure) as raised:
            callable_()
        if code:
            self.assertEqual(raised.exception.code, code)

    def test_exact_unicode_and_entire_counter_context_c1_round_trip(self):
        packet = packet_fixture(); original = deepcopy(packet)
        frame = t.build_frame(packet)
        restored = t.validate_transport(frame, packet)
        self.assertEqual(canonical_bytes(restored), canonical_bytes(original))
        self.assertEqual(packet, original)
        self.assertTrue(frame['context_table']); self.assertTrue(frame['text_table'])
        self.assertEqual(restored['sources'][1]['purpose'], 'SYNTH against')
        self.assertEqual(restored['documentary_evidence'], original['documentary_evidence'])
        for segment in restored['segments']:
            self.assertEqual(text_sha(segment['text']), segment['text_sha256'])

    def test_json_input_after_projection_remains_deterministic_and_compact(self):
        packet = packet_fixture()
        frame = t.build_frame(packet)
        self.assertEqual(frame, t.build_frame(deepcopy(packet)))
        self.assertLess(len(canonical_bytes(frame)), len(canonical_bytes(packet)))
        self.assertEqual(t.validate_transport(json.loads(canonical_bytes(frame)), packet), packet)

    def test_repeated_small_strings_are_interned_only_when_net_cost_is_positive(self):
        packet = packet_fixture()
        rare = 'SYNTH short text with 40 bytes exactly!!!'
        frequent = 'SYNTH frequent digest-sized value with many exact repetitions. 64'
        packet['global_context']['rare'] = [rare, rare]
        packet['global_context']['frequent'] = [frequent] * 12
        frame = t.build_frame(packet)
        shared = {entry['value'] for entry in frame['text_table'].values()}
        self.assertNotIn(rare, shared)
        self.assertIn(frequent, shared)
        self.assertEqual(t.validate_transport(frame, packet), packet)

    def test_real_protocol_packet_and_output_schema_are_unchanged(self):
        from atelier.tests.test_verification_protocol import plan_fixture
        plan = plan_fixture()
        for batch in (plan['batches'][0], next(x for x in plan['batches'] if x['stage'] == 'claims')):
            packet = p.packet_for(plan, batch)
            before = p.response_schema(batch['stage'], packet)
            self.assertEqual(t.validate_transport(t.build_frame(packet), packet), packet)
            self.assertEqual(p.response_schema(batch['stage'], packet), before)
            self.assertEqual(before['$defs']['segment_id']['enum'], [x['segment_id'] for x in packet['segments']])

    def test_original_policy_instructions_prefix_and_three_hashes_are_bound(self):
        packet = packet_fixture()
        original = p.prompt_for('claims', packet)
        prefix = original[:-len(json.dumps(packet, ensure_ascii=False, sort_keys=True))]
        prompt = t.build_prompt('claims', packet)
        self.assertTrue(prompt.startswith(prefix + t.TRANSPORT_INSTRUCTION))
        serialized = prompt[len(prefix + t.TRANSPORT_INSTRUCTION):]
        frame = json.loads(serialized)
        self.assertEqual(serialized.encode(), canonical_bytes(frame))
        receipt = t.receipt('claims', packet)
        self.assertEqual(receipt['input_sha256'], text_sha(prompt))
        self.assertEqual(receipt['logical_input_sha256'], text_sha(original))
        self.assertEqual(receipt['logical_packet_sha256'], sha256_digest(packet))
        self.assertEqual(receipt['frame_sha256'], sha256_digest(frame))
        self.assertNotEqual(receipt['input_sha256'], receipt['logical_input_sha256'])
        self.assertFalse(receipt['provider_capacity_attested'])
        self.assertEqual(receipt['input_bytes'], len(prompt.encode()))
        self.assertEqual(receipt['input_codepoints'], len(prompt))

    def test_ids_and_id_arrays_remain_literals_even_if_long_and_repeated(self):
        packet = packet_fixture()
        long_id = 'SYNTH-long-identifier-' * 12
        for assertion in packet['assertions']:
            assertion['assertion_id'] = long_id
        packet['evidence_findings'][0]['segment_ids'] = [long_id] * 2
        frame = t.build_frame(packet)
        self.assertTrue(all(x['assertion_id'] == long_id for x in frame['packet']['assertions']))
        self.assertEqual(frame['packet']['evidence_findings'][0]['segment_ids'], [long_id] * 2)

    def test_source_passage_identity_digest_and_exact_segment_text_must_match(self):
        for field, value in [('source_id', 'SYNTH-other'), ('version_id', 'SYNTH-other'),
                             ('passage_id', True), ('source_sha256', '0' * 64),
                             ('passage_sha256', '0' * 64), ('text_sha256', '0' * 64),
                             ('segment_id', 'SYNTH-unbound'), ('text', 'SYNTH rewritten')]:
            with self.subTest(field=field):
                packet = packet_fixture(); packet['segments'][0][field] = value
                self.assert_refused(lambda: t.build_frame(packet))

    def test_codepoint_bounds_and_bool_offsets_are_refused(self):
        for start, end in ((-1, 2), (1, 1), (0, 100000), (True, 2), (0, False)):
            with self.subTest(start=start, end=end):
                packet = packet_fixture(); packet['segments'][0].update(start=start, end=end)
                self.assert_refused(lambda: t.build_frame(packet), 'transport_slice_bounds')
        packet = packet_fixture(); packet['segments'][0]['offset_unit'] = 'utf8_bytes'
        self.assert_refused(lambda: t.build_frame(packet), 'transport_slice_bounds')

    def test_wrong_or_missing_table_reference_and_mixed_magic_dict_are_refused(self):
        packet = packet_fixture(); frame = t.build_frame(packet)
        for replacement in ({t.CONTEXT_REF: 'c999'}, {t.CONTEXT_REF: True},
                            {t.CONTEXT_REF: 'c0', 'foreign': 'SYNTH injected'}):
            with self.subTest(form=replacement):
                bad = deepcopy(frame); bad['packet']['assertions'][0]['context'] = replacement
                self.assert_refused(lambda: t.validate_transport(bad, packet))
        bad = deepcopy(frame); bad['context_table']['c0']['value'] = {t.TEXT_REF: 't999'}
        self.assert_refused(lambda: t.validate_transport(bad, packet))

    def test_context_cycles_and_nested_contexts_are_refused(self):
        packet = packet_fixture(); frame = t.build_frame(packet)
        for value in ({t.CONTEXT_REF: 'c0'}, {'nested': {t.CONTEXT_REF: 'c0'}}):
            with self.subTest(value=value):
                bad = deepcopy(frame); bad['context_table']['c0']['value'] = value
                self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_context_reference_or_cycle')

    def test_valid_hash_but_unused_foreign_tables_and_extra_keys_are_refused(self):
        packet = packet_fixture(); frame = t.build_frame(packet)
        bad = deepcopy(frame); alias = 't' + str(len(bad['text_table']))
        bad['text_table'][alias] = {'sha256': text_sha('SYNTH FOREIGN'), 'value': 'SYNTH FOREIGN'}
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_unused_table_entries')
        bad = deepcopy(frame); alias = 'c' + str(len(bad['context_table']))
        bad['context_table'][alias] = {'sha256': sha256_digest({'foreign': 'SYNTH'}), 'value': {'foreign': 'SYNTH'}}
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_unused_table_entries')
        bad = deepcopy(frame); bad['foreign'] = 'SYNTH instructions'
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_frame_shape')
        bad = deepcopy(frame); bad['text_table']['t0']['foreign'] = 'SYNTH'
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_table_entry_shape')

    def test_changed_text_or_context_hash_even_after_rehash_is_refused(self):
        packet = packet_fixture(); frame = t.build_frame(packet)
        bad = deepcopy(frame); bad['text_table']['t0']['value'] += 'SYNTH mutation'
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_text_table_sha')
        bad['text_table']['t0']['sha256'] = text_sha(bad['text_table']['t0']['value'])
        self.assert_refused(lambda: t.validate_transport(bad, packet))
        bad = deepcopy(frame); bad['logical_packet_sha256'] = '0' * 64
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_logical_sha_or_contract')

    def test_slice_markers_outside_segments_and_integer_true_are_refused(self):
        packet = packet_fixture(); frame = t.build_frame(packet)
        bad = deepcopy(frame); bad['packet']['segments'][0]['text'] = {t.PASSAGE_SLICE: 1}
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_slice_marker')
        bad = deepcopy(frame); bad['packet']['global_context']['title'] = {t.PASSAGE_SLICE: True}
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_reserved_key_collision')
        bad = deepcopy(frame); bad['packet']['segments'][0]['start'] += 1
        self.assert_refused(lambda: t.validate_transport(bad, packet), 'transport_slice_text_sha')

    def test_original_reserved_dict_duplicate_binding_and_non_json_are_refused(self):
        packet = packet_fixture(); packet['global_context']['foreign'] = {t.TEXT_REF: 't0'}
        self.assert_refused(lambda: t.build_frame(packet), 'transport_reserved_key_collision')
        packet = packet_fixture(); packet['sources'].append(deepcopy(packet['sources'][0]))
        self.assert_refused(lambda: t.build_frame(packet), 'transport_duplicate_source_binding')
        packet = packet_fixture(); packet['global_context']['floating'] = 1.5
        self.assert_refused(lambda: t.build_frame(packet), 'transport_non_json_or_float')

    def test_byte_and_codepoint_input_limits_at_exact_bound_and_one_below(self):
        packet = packet_fixture(); prompt = t.build_prompt('claims', packet)
        self.assertGreater(len(prompt.encode()), len(prompt))
        with patch.object(t, 'MAX_WIRE_BYTES', len(prompt.encode())), patch.object(t, 'MAX_WIRE_CODEPOINTS', len(prompt)):
            self.assertEqual(t.build_prompt('claims', packet), prompt)
        with patch.object(t, 'MAX_WIRE_BYTES', len(prompt.encode()) - 1):
            self.assert_refused(lambda: t.build_prompt('claims', packet), 'transport_wire_prompt_limit')
        with patch.object(t, 'MAX_WIRE_CODEPOINTS', len(prompt) - 1):
            self.assert_refused(lambda: t.build_prompt('claims', packet), 'transport_wire_prompt_limit')

    def test_source_too_large_is_refused_without_truncation_or_call(self):
        packet = packet_fixture('SYNTH exact giant source. ' * 55000)
        before = deepcopy(packet)
        self.assert_refused(lambda: t.build_prompt('evidence', packet), 'transport_wire_prompt_limit')
        self.assertEqual(packet, before)
        packet = packet_fixture('SYNTH impossible capture. ' * 190000)
        self.assert_refused(lambda: t.build_frame(packet), 'transport_capture_limit')

    def test_immutable_receipt_and_frame_are_bound_and_refuse_changed_saved_file(self):
        packet = packet_fixture()
        with tempfile.TemporaryDirectory(prefix='dv-SYNTH-lossless-') as tmp:
            directory = Path(tmp)
            receipt = t.write_receipt('claims', packet, directory)
            self.assertEqual(receipt, t.write_receipt('claims', packet, directory))
            frame = json.loads((directory / 'transport-frame.json').read_bytes())
            self.assertEqual(sha256_digest(frame), receipt['frame_sha256'])
            self.assertEqual(t.validate_transport(frame, packet), packet)
            receipt_file = directory / 'transport-receipt.json'
            receipt_file.write_bytes(b'SYNTH tampered')
            self.assert_refused(lambda: t.write_receipt('claims', packet, directory), 'transport_saved_file_changed')
            self.assertEqual(receipt_file.read_bytes(), b'SYNTH tampered')

    def test_path_traversal_is_refused(self):
        packet = packet_fixture()
        with tempfile.TemporaryDirectory(prefix='dv-SYNTH-lossless-path-') as tmp:
            directory = Path(tmp)
            self.assert_refused(lambda: t.write_receipt('claims', packet, directory / '..' / 'outside'),
                                'transport_absolute_directory_required')

    def test_link_file_is_refused_when_host_allows_link_creation(self):
        packet = packet_fixture()
        with tempfile.TemporaryDirectory(prefix='dv-SYNTH-lossless-link-') as tmp:
            directory = Path(tmp)
            target = directory / 'SYNTH-untouched.txt'; target.write_bytes(b'SYNTH original')
            link = directory / 'transport-frame.json'
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest('Host does not permit SYNTH symlink creation')
            self.assert_refused(lambda: t.write_receipt('claims', packet, directory), 'transport_link_refused')
            self.assertEqual(target.read_bytes(), b'SYNTH original')

    def test_stage_and_original_instruction_suffix_mismatch_are_refused(self):
        packet = packet_fixture()
        self.assert_refused(lambda: t.build_prompt('publication', packet), 'transport_stage')
        with patch.object(p, 'prompt_for', return_value='SYNTH malformed instructions'):
            self.assert_refused(lambda: t.build_prompt('claims', packet), 'transport_original_json_suffix_mismatch')


if __name__ == '__main__':
    unittest.main()
