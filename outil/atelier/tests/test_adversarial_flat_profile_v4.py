"""Pure SYNTH profile selection/grouping invariants; no native judgment or ORM."""
from copy import deepcopy
from types import ModuleType
import unittest
from unittest.mock import patch

from django.core.exceptions import ValidationError
from atelier.services import verification_counterevidence as counter
from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.tests import test_adversarial_response_v3 as previous


ABSENT_V3_GOLDEN = {
    False: ('83457f960fb47242ec214e518f5188def028292c8c725b4d50a4889f79acabf8',
            '4e345ba3b916f73d4b9c38f01bc329738698f1f7db3fbb28c80d7d24221f25fa',
            'b313ac37494ab67548deb3475d8289037015fe22ff2fc05e735f8a8d6074402c'),
    True: ('3e21859f60773c42fbd8f6bafb71888e714874c847bef13b46d2519471c202fa',
           'bbb8160035edea7d61b420f6e64e0222cb3a889648f48198716ac7a511834727',
           'c06a20cc13a726941e5dffeae01a8dcf4d04e43bbf16e6e501afbe1366bea346')}
OMITTED = object()


def classification_stub():
    module = ModuleType(counter.__package__ + '.proof_classification')
    # Unit-test the grouping/wire contract only: never a production provenance pass.
    module.validate_classification = lambda *args, **kwargs: None
    return patch.dict('sys.modules', {module.__name__: module})


def plan_for(count=17, *, profile=counter.SCHEMA_PROFILE_V4,
             response=counter.ADVERSARIAL_RESPONSE_V3, max_claims=OMITTED,
             dependency=True, two_passages=False, supports=1, nonrequired=False):
    snapshot, classification, first = previous.fixture(dependency)
    original = snapshot['claims'][0]
    judgment = first['claims'][0]
    snapshot['claims'] = [{**deepcopy(original), 'id': f'SYNTH-flat-{i:03d}', 'location': f'/SYNTH/{i}'}
                          for i in range(count)]
    classification['claims'] = [{'claim_id': claim['id'], 'layer': 'semantic', 'necessary_for_conclusion': True}
                                for claim in snapshot['claims']]
    first['claims'] = [{**deepcopy(judgment), **claim, 'verdict': 'INDETERMINE',
                        'support': deepcopy(judgment['support']) * supports}
                       for claim in snapshot['claims']]
    if nonrequired:
        extra = {**deepcopy(original), 'id': 'SYNTH-outside-conclusion', 'location': '/SYNTH/outside'}
        snapshot['claims'].append(extra)
        classification['claims'].append({'claim_id': extra['id'], 'layer': 'semantic', 'necessary_for_conclusion': False})
    if two_passages:
        second = deepcopy(snapshot['sources'][0]['passages'][0]); second['id'] = 2
        snapshot['sources'][0]['passages'].append(second)
    first['fingerprint'] = sha256_digest(snapshot)
    kwargs = {'revision_id': 1, 'job_id': 2, 'job_key': 'SYNTH-flat-job', 'owner_id': 3,
              'first_receipt_id': 4, 'model': 'SYNTH', 'effort': 'none'}
    if response is not OMITTED:
        kwargs['response_contract'] = response
    if profile is not OMITTED:
        kwargs['schema_profile'] = profile
    if max_claims is not OMITTED:
        kwargs['max_claims'] = max_claims
    with classification_stub():
        return counter.build_adversarial_plan(snapshot, classification, first, **kwargs)


def raw_for(plan, batch=None):
    batch = batch or plan['batches'][0]
    temporary = {**plan, 'batches': [batch]}
    raw = previous.raw_for(temporary)
    for value in raw['claims'].values():
        value['conclusion'] = 'indeterminate'
    for value in raw['axes'].values():
        value['searched_passage_ids'] = deepcopy(batch['searched_passage_ids'])
    return raw


class AdversarialFlatProfileV4Tests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_for(two_passages=True)
        self.batch = self.plan['batches'][0]
        self.raw = raw_for(self.plan)

    def rejected(self, plan=None, batch=None, raw=None):
        with self.assertRaises(ValidationError):
            counter.validate_adversarial_response(plan or self.plan, batch or self.batch,
                                                  self.raw if raw is None else raw)

    def test_profile_default_and_maximum_ten(self):
        self.assertEqual(self.plan['schema_profile'], counter.SCHEMA_PROFILE_V4)
        self.assertEqual(self.plan['max_claims'], 10)
        self.assertEqual([len(b['claim_ids']) for b in self.plan['batches']], [10, 7])

    def test_profile_explicit_smaller_batch_remains_honored(self):
        self.assertEqual(plan_for(max_claims=8)['max_claims'], 8)

    def test_profile_ten_accepted(self):
        self.assertEqual(plan_for(max_claims=10)['max_claims'], 10)

    def test_profile_eleven_refused(self):
        with self.assertRaises(ValidationError): plan_for(max_claims=11)

    def test_absent_profile_default_eight(self):
        self.assertEqual(plan_for(profile=OMITTED)['max_claims'], 8)

    def test_absent_profile_nine_refused(self):
        with self.assertRaises(ValidationError): plan_for(profile=OMITTED, max_claims=9)

    def test_absent_profile_ten_refused(self):
        with self.assertRaises(ValidationError): plan_for(profile=OMITTED, max_claims=10)

    def test_explicit_none_max_refused(self):
        with self.assertRaises(ValidationError): plan_for(max_claims=None)

    def test_bool_max_refused(self):
        with self.assertRaises(ValidationError): plan_for(max_claims=True)

    def test_zero_max_refused(self):
        with self.assertRaises(ValidationError): plan_for(max_claims=0)

    def test_unknown_profile_refused(self):
        with self.assertRaises(ValidationError): plan_for(profile='dv.adversarial-schema/999')

    def test_untyped_profile_refused(self):
        with self.assertRaises(ValidationError): plan_for(profile={'schema': 4})

    def test_profile_requires_explicit_response3(self):
        with self.assertRaises(ValidationError): plan_for(response=OMITTED)

    def test_profile_requires_not_none_response(self):
        with self.assertRaises(ValidationError): plan_for(response=None)

    def test_profile_rejects_response2(self):
        with self.assertRaises(ValidationError): plan_for(response=counter.ADVERSARIAL_RESPONSE_V2)

    def test_profile_without_conclusion_still_explicit_v3(self):
        plan = plan_for(dependency=False)
        raw = raw_for(plan)
        self.assertIs(counter.validate_adversarial_response(plan, plan['batches'][0], raw), raw)

    def test_none_profile_call_has_no_wire_selector(self):
        plan = plan_for(profile=None)
        self.assertNotIn('schema_profile', plan)
        self.assertNotIn('schema_profile', plan['batches'][0]['packet'])

    def test_none_profile_call_identical_to_omission(self):
        self.assertEqual(plan_for(profile=None), plan_for(profile=OMITTED))

    def test_profile_plan_packet_digest_and_input_changed_only_opt_in(self):
        old = plan_for(profile=OMITTED, max_claims=8)
        new = plan_for(max_claims=8)
        self.assertEqual(old['snapshot'], new['snapshot'])
        self.assertEqual(old['first_result_digest'], new['first_result_digest'])
        self.assertEqual(old['semantic_claim_ids'], new['semantic_claim_ids'])
        self.assertNotEqual(old['plan_digest'], new['plan_digest'])
        self.assertNotEqual(old['batches'][0]['packet_digest'], new['batches'][0]['packet_digest'])
        self.assertNotEqual(old['batches'][0]['input_bytes'], new['batches'][0]['input_bytes'])

    def test_exact_coverage_once_for_all_four_counts(self):
        plans = [plan_for(count) for count in (77, 80, 74, 86)]
        self.assertEqual([len(p['batches']) for p in plans], [8, 8, 8, 9])
        self.assertEqual(sum(len(p['semantic_claim_ids']) for p in plans), 317)
        for plan in plans:
            got = [cid for b in plan['batches'] for cid in b['claim_ids']]
            self.assertEqual(got, plan['semantic_claim_ids'])
            self.assertEqual(len(got), len(set(got)))

    def test_nonrequired_semantic_claim_not_added_to_new_grouping(self):
        plan = plan_for(nonrequired=True)
        self.assertNotIn('SYNTH-outside-conclusion', plan['semantic_claim_ids'])
        self.assertEqual(len(plan['semantic_claim_ids']), 17)

    def test_full_sources_axes_scope_and_references_for_each_batch(self):
        for batch in self.plan['batches']:
            packet = batch['packet']
            self.assertEqual(packet['sources'], self.plan['snapshot']['sources'])
            self.assertEqual(packet['axes'], list(counter.AXES))
            self.assertEqual(packet['schema_profile'], counter.SCHEMA_PROFILE_V4)
            self.assertEqual(packet['support_references'], {c['id']: {str(i): sha256_digest(s)
                for i,s in enumerate(c['support'])} for c in packet['claims']})

    def test_input_wire_bound_still_fixed(self):
        self.assertEqual(self.plan['max_input_bytes'], 4 * 1024 * 1024)
        for batch in self.plan['batches']:
            self.assertEqual(batch['input_bytes'], len(canonical_bytes(batch['packet'])))
            self.assertLessEqual(batch['input_bytes'], self.plan['max_input_bytes'])

    def test_output_wire_bound_still_fixed(self):
        self.assertEqual(self.plan['max_output_bytes'], 128 * 1024)
        raw = deepcopy(self.raw); raw['limitations'] = ['x' * (128 * 1024)]
        self.rejected(raw=raw)

    def test_profile_plan_rebuild_exact(self):
        with classification_stub():
            self.assertIs(counter.validate_adversarial_plan(self.plan, self.plan['classification']), self.plan)

    def test_profile_plan_explicit_none_selector_refused(self):
        changed = deepcopy(self.plan); changed['schema_profile'] = None
        with classification_stub(), self.assertRaises(ValidationError):
            counter.validate_adversarial_plan(changed, changed['classification'])

    def test_profile_plan_unknown_selector_refused(self):
        changed = deepcopy(self.plan); changed['schema_profile'] = '999'
        with classification_stub(), self.assertRaises(ValidationError):
            counter.validate_adversarial_plan(changed, changed['classification'])

    def test_profile_packet_missing_selector_refused(self):
        batch = deepcopy(self.batch); batch['packet'].pop('schema_profile'); self.rejected(batch=batch)

    def test_profile_packet_unknown_selector_refused(self):
        batch = deepcopy(self.batch); batch['packet']['schema_profile'] = '999'; self.rejected(batch=batch)

    def test_profile_packet_none_selector_refused(self):
        batch = deepcopy(self.batch); batch['packet']['schema_profile'] = None; self.rejected(batch=batch)

    def test_packet_cannot_introduce_unselected_profile(self):
        plan = plan_for(profile=OMITTED)
        batch = deepcopy(plan['batches'][0]); batch['packet']['schema_profile'] = counter.SCHEMA_PROFILE_V4
        self.rejected(plan=plan, batch=batch, raw=raw_for(plan))

    def test_response_extra_schema_selector_refused(self):
        raw = deepcopy(self.raw); raw['schema_profile'] = counter.SCHEMA_PROFILE_V4; self.rejected(raw=raw)

    def test_raw3_and_internal_expansion_remain_distinct_and_unchanged(self):
        before = deepcopy(self.raw)
        self.assertIs(counter.validate_adversarial_response(self.plan, self.batch, self.raw), self.raw)
        expanded = counter.expand_adversarial_response(self.plan, self.batch, self.raw)
        self.assertEqual(self.raw, before)
        self.assertIsInstance(self.raw['claims'], dict)
        self.assertIsInstance(expanded['claims'], list)

    def test_report_responses_keep_raw3_canonical(self):
        responses = {b['batch_id']: raw_for(self.plan, b) for b in self.plan['batches']}
        report = counter.aggregate_adversarial(self.plan, responses)
        self.assertEqual(report['responses'], list(responses.values()))
        self.assertTrue(all(c['conclusion'] == 'indeterminate' for raw in report['responses'] for c in raw['claims'].values()))
        self.assertFalse(report['summary']['counterevidence_complete'])

    def test_searched_order_exact(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = [2,1]
        self.rejected(raw=raw)

    def test_searched_missing_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = [1]; self.rejected(raw=raw)

    def test_searched_duplicate_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = [1,2,2]; self.rejected(raw=raw)

    def test_searched_bool_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = [True,2]; self.rejected(raw=raw)

    def test_searched_float_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = [1.0,2]
        with self.assertRaises((ValidationError, ValueError)):
            counter.validate_adversarial_response(self.plan, self.batch, raw)

    def test_searched_string_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = ['1',2]; self.rejected(raw=raw)

    def test_searched_nonlist_refused(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['searched_passage_ids'] = None; self.rejected(raw=raw)

    def test_unavailable_does_not_hide_scope(self):
        raw = deepcopy(self.raw)
        raw['axes'][counter.AXES[0]].update(status='unavailable', searched_passage_ids=[])
        self.rejected(raw=raw)

    def test_required_axis_cannot_be_not_applicable(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['status'] = 'not_applicable'; self.rejected(raw=raw)

    def test_optional_axis_not_found_stays_refused(self):
        raw = deepcopy(self.raw); raw['axes']['affiliation_change']['status'] = 'not_found'; self.rejected(raw=raw)

    def test_found_requires_findings(self):
        raw = deepcopy(self.raw); raw['axes'][counter.AXES[0]]['status'] = 'found'; self.rejected(raw=raw)

    def test_no_objection_cannot_cover_adverse_support(self):
        raw = deepcopy(self.raw)
        first = next(iter(raw['claims'].values())); first['conclusion'] = 'no_objection'
        first['support_relations']['0']['relation'] = 'contradicts'; self.rejected(raw=raw)

    def test_support_sha_stays_exact(self):
        raw = deepcopy(self.raw); next(iter(raw['claims'].values()))['support_relations']['0']['support_sha256'] = 'b'*64
        self.rejected(raw=raw)

    def test_thirteen_selected_supports_are_all_referenced(self):
        plan = plan_for(count=10, supports=13)
        raw = raw_for(plan)
        self.assertIs(counter.validate_adversarial_response(plan, plan['batches'][0], raw), raw)
        self.assertTrue(all(len(c['support_relations']) == 13 for c in raw['claims'].values()))

    def test_claim_missing_stays_refused(self):
        raw = deepcopy(self.raw); raw['claims'].pop(next(iter(raw['claims']))); self.rejected(raw=raw)

    def test_claim_extra_stays_refused(self):
        raw = deepcopy(self.raw); raw['claims']['foreign'] = deepcopy(next(iter(raw['claims'].values()))); self.rejected(raw=raw)

    def test_eight_axes_stay_exact(self):
        raw = deepcopy(self.raw); raw['axes'].pop(counter.AXES[0]); self.rejected(raw=raw)

    def test_findings_are_still_exact_public_quotes(self):
        raw = deepcopy(self.raw); cid = self.batch['claim_ids'][0]
        raw['axes'][counter.AXES[0]].update(status='found', findings=[{'claim_id':cid, 'passage_id':1,
            'quote':'SYNTH unreceived invented quote', 'relation':'qualifies', 'reason':'SYNTH'}])
        self.rejected(raw=raw)

    def test_favorable_quota_not_created_by_grouping(self):
        plan = plan_for(count=77)
        self.assertTrue(all(c['verdict'] == 'INDETERMINE' for c in plan['first_result']['claims']))
        self.assertEqual(plan['authorization'], 'NOT_AUTHORIZED')
        self.assertEqual(plan['allocation_proposal']['actual_phases_launched'], 0)

    def test_old_legacy1_2_goldens_byte_identical(self):
        for dependency in (False, True):
            plan = previous.plan_for(dependency)
            raw = previous.raw_for(plan)
            report = counter.aggregate_adversarial(plan, {plan['batches'][0]['batch_id']: raw})
            golden = previous.LEGACY_GOLDEN['2' if dependency else '1']
            self.assertEqual(sha256_digest(plan), golden['plan_sha256'])
            self.assertEqual(sha256_digest(raw), golden['response_sha256'])
            self.assertEqual(sha256_digest(report), golden['report_sha256'])

    def test_old_v3_profile_absent_goldens_byte_identical(self):
        for dependency, expected in ABSENT_V3_GOLDEN.items():
            plan = previous.plan_for(dependency, counter.ADVERSARIAL_RESPONSE_V3)
            raw = previous.raw_for(plan)
            report = counter.aggregate_adversarial(plan, {plan['batches'][0]['batch_id']:raw})
            self.assertEqual(tuple(sha256_digest(value) for value in (plan,raw,report)), expected)


if __name__ == '__main__':
    unittest.main()
