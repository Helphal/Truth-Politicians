"""SYNTH transport witnesses, not a political/semantic acceptance campaign."""
from copy import deepcopy
from dataclasses import replace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from atelier.services import verification as v
from atelier.services import verification_protocol as p
from atelier.services.canonical import sha256_digest


def snapshot_fixture():
    text = 'SYNTH : le vote n’est pas adopté. Valeur 12,5 % ; élue 👩\u200d🔬.\nContre-élément conservé.'
    content = {'title': 'SYNTH vote non adopté.', 'short_answer': 'SYNTH avec réserve.',
               'main_caveat': 'SYNTH aucune vérité réelle.', 'reasoning': '', 'result': 'SYNTH',
               'method_version': 'SYNTH/1', 'public_payload': {'vote': {'person': 'SYNTH Élise',
                    'date': '2000-01-01', 'chamber': 'SYNTH', 'measure': 125, 'unit': 'SYNTH'}},
               'source_snapshot': [{'source_id': 'SYNTH-SOURCE', 'version_id': 'SYNTH-v1',
                                    'sha256': 'a' * 64, 'public_url': 'https://example.invalid/SYNTH'}]}
    passage = {'id': 31, 'exact_text': text, 'locator': 'SYNTH p1', 'context': 'SYNTH condition non tronquée.',
               'public_url': 'https://example.invalid/SYNTH', 'text_state': 'known',
               'transcription_status': 'source_text', 'public_access_status': 'available'}
    source = {'reference': deepcopy(content['source_snapshot'][0]), 'state': 'available',
              'latest_sha256': 'a' * 64, 'document_date': '2000-01-01', 'captured_at': '2000-01-01T00:00:00Z',
              'rights': 'open', 'access': 'active', 'passages': [passage], 'integrity': 'synthetic_no_original'}
    return {'object': 'SYNTH-OBJECT', 'kind': 'speech_vote', 'scope': 'synthetic', 'revision': 1,
            'policy': v.POLICY, 'extractor': v.EXTRACTOR, 'content': content, 'sources': [source],
            'claims': list(v._scalars(content))}


def plan_fixture(snapshot=None, **kwargs):
    return p.build_plan(snapshot or snapshot_fixture(), job_id=1, job_key='SYNTH-job',
                        revision_id=1, owner_id=1, model='SYNTH', effort='none', **kwargs)


def claims_raw(plan, batch, verdict='ETAYE_DANS_PERIMETRE'):
    sid = plan['registry']['segments'][0]['segment_id']
    return {'protocol': p.PROTOCOL, 'batch_id': batch['batch_id'], 'coverage_statement': 'SYNTH tous présentés.',
            'limitations': ['SYNTH, aucun jugement réel.'], 'claims': [
                {'assertion_id': aid, 'nature': 'fact', 'verdict': verdict,
                 'support': [{'segment_id': sid, 'explanation': 'Lien explicitement fictif SYNTH.'}],
                 'against': [], 'limits': ['SYNTH'], 'reason': 'Jugement du runner SYNTH.', 'action': 'conserver'}
                for aid in batch['assertion_ids']]}


def evidence_raw(plan, batch):
    packet = p.packet_for(plan, batch)
    findings = []
    for source in packet['sources']:
        ref = source['reference']
        ids = [s['segment_id'] for s in packet['segments']
               if (s['source_id'], s['version_id']) == (ref['source_id'], ref['version_id'])]
        findings.append({'source_id': ref['source_id'], 'version_id': ref['version_id'],
                         'finding': 'Lecture fictive SYNTH.', 'segment_ids': ids, 'limits': ['SYNTH']})
    return {'protocol': p.PROTOCOL, 'batch_id': batch['batch_id'], 'sources': findings,
            'counter_search': ['Inspection SYNTH des contre-éléments présents.'], 'limitations': ['SYNTH seulement.']}


class ProtocolTests(SimpleTestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.batch = next(b for b in self.plan['batches'] if b['stage'] == 'claims')

    def test_unicode_codepoints_preserve_accent_negation_number_and_emoji(self):
        snap = snapshot_fixture()
        limits = replace(p.Limits(), max_segment_codepoints=8)
        registry = p.build_registry(snap, limits)
        text = snap['sources'][0]['passages'][0]['exact_text']
        self.assertEqual(''.join(s['text'] for s in registry['segments']), text)
        for segment in registry['segments']:
            self.assertEqual(segment['offset_unit'], 'unicode_codepoints')
            self.assertEqual(text[segment['start']:segment['end']], segment['text'])
        self.assertGreater(len(text.encode('utf-8')), len(text))
        p.validate_registry(snap, registry, limits)

    def test_tampered_registry_families_all_refuse_and_authentic_registry_passes(self):
        cases = {'source_id': 'SYNTH-other', 'version_id': 'SYNTH-v2', 'passage_id': 99,
                 'start': 1, 'end': 2, 'text': 'SYNTH adopté 125 %',
                 'offset_unit': 'utf8_bytes', 'passage_sha256': 'b'*64, 'source_sha256': 'b'*64}
        for field, value in cases.items():
            with self.subTest(field=field):
                bad = deepcopy(self.plan['registry']); bad['segments'][0][field] = value
                with self.assertRaises(ValidationError):
                    p.validate_registry(self.plan['snapshot'], bad)
        p.validate_registry(self.plan['snapshot'], self.plan['registry'])

    def test_other_snapshot_and_truncated_passage_refuse(self):
        for transform in [lambda s: s['sources'][0]['passages'][0].update(exact_text='SYNTH'),
                          lambda s: s.update(revision=2)]:
            snap = deepcopy(self.plan['snapshot']); transform(snap)
            with self.assertRaises(ValidationError):
                p.validate_registry(snap, self.plan['registry'])

    def test_duplicate_passage_and_registry_capture_limits_refuse(self):
        snap = snapshot_fixture(); snap['sources'][0]['passages'].append(deepcopy(snap['sources'][0]['passages'][0]))
        with self.assertRaisesRegex(ValidationError, 'duplicate'):
            p.build_registry(snap)
        with self.assertRaisesRegex(ValidationError, 'capture_limit'):
            p.build_registry(snapshot_fixture(), replace(p.Limits(), max_registry_bytes=1))
        with self.assertRaisesRegex(ValidationError, 'enum_limit'):
            p.build_registry(snapshot_fixture(), replace(p.Limits(), max_registry_segments=1, max_segment_codepoints=8))

    def test_context_preserves_person_date_chamber_measure_and_reserve(self):
        assertion = next(x for x in self.plan['assertions'] if x['location'].endswith('/measure'))
        obj = assertion['context']['object']
        self.assertEqual(obj['person'], 'SYNTH Élise'); self.assertEqual(obj['unit'], 'SYNTH')
        packet = p.packet_for(self.plan, self.batch)
        self.assertEqual(packet['global_context']['main_caveat'], 'SYNTH aucune vérité réelle.')
        self.assertIn('n’est pas adopté', packet['sources'][0]['passages'][0]['exact_text'])

    def test_all_assertions_exact_and_metadata_predicate_unchanged(self):
        expected = self.plan['snapshot']['claims']
        self.assertEqual([x['claim_id'] for x in self.plan['assertions']], [x['id'] for x in expected])
        for claim, assertion in zip(expected, self.plan['assertions']):
            self.assertEqual(assertion['structural_metadata'], v.structural_metadata(claim, self.plan['snapshot']))
        self.assertFalse(next(x for x in self.plan['assertions'] if x['location'].endswith('/date'))['structural_metadata'])

    def test_partition_disjoint_complete_and_stable_across_limits(self):
        tiny = plan_fixture(limits=replace(p.Limits(), max_claims=2))
        ids = [x for b in tiny['batches'] if b['stage']=='claims' for x in b['assertion_ids']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {x['assertion_id'] for x in tiny['assertions'] if not x['structural_metadata']})
        self.assertEqual([x['assertion_id'] for x in tiny['assertions']], [x['assertion_id'] for x in self.plan['assertions']])
        self.assertEqual(tiny['allocation_proposal']['actual_phases_launched'], 0)

    def test_partition_and_measurement_tampering_refused_even_with_new_digest(self):
        tiny = plan_fixture(limits=replace(p.Limits(), max_claims=2))
        for change in ('duplicate', 'missing', 'foreign', 'measure'):
            bad = deepcopy(tiny)
            claims = [b for b in bad['batches'] if b['stage']=='claims']
            if change == 'duplicate': claims[1]['assertion_ids'][0] = claims[0]['assertion_ids'][0]
            if change == 'missing': claims[0]['assertion_ids'].pop()
            if change == 'foreign': claims[0]['assertion_ids'][0] = True
            if change == 'measure': claims[0]['measurements']['input_bytes'] += 1
            bad.pop('plan_digest'); bad['plan_digest'] = sha256_digest(bad)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                p.validate_plan(bad)

    def test_bounded_schema_and_context_refuse_instead_of_truncating(self):
        with self.assertRaisesRegex(ValidationError, 'limit'):
            plan_fixture(limits=replace(p.Limits(), max_schema_bytes=10))
        with self.assertRaisesRegex(ValidationError, 'limit'):
            plan_fixture(limits=replace(p.Limits(), max_input_bytes=100))

    def test_exact_translation_and_raw_immutable_with_support_and_against(self):
        raw = claims_raw(self.plan, self.batch, 'CONFLIT_DE_SOURCES')
        raw['claims'][0]['against'] = deepcopy(raw['claims'][0]['support'])
        before = deepcopy(raw)
        result, mapping = p.translate_claims(self.plan, self.batch, raw)
        self.assertEqual(raw, before)
        self.assertEqual(result['claims'][0]['against'][0]['quote'], self.plan['registry']['segments'][0]['text'])
        self.assertEqual(result['claims'][0]['verdict'], 'CONFLIT_DE_SOURCES')
        self.assertFalse(mapping['mechanically_supported'])
        self.assertIn('against', [x['family'] for x in mapping['references']])

    def test_foreign_absent_boolean_or_repeated_reference_refused(self):
        for value in (True, 0, 'seg-absent'):
            raw = claims_raw(self.plan, self.batch); raw['claims'][0]['support'][0]['segment_id'] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                p.translate_claims(self.plan, self.batch, raw)
        for family in ('support', 'against'):
            raw = claims_raw(self.plan, self.batch)
            raw['claims'][0][family] = [deepcopy(raw['claims'][0]['support'][0])] * 2
            with self.assertRaises(ValidationError): p.translate_claims(self.plan, self.batch, raw)

    def test_invalid_against_refuses_with_valid_support_witness(self):
        raw = claims_raw(self.plan, self.batch)
        p.translate_claims(self.plan, self.batch, raw)
        raw['claims'][0]['against'] = [{'segment_id': 'seg-absent', 'explanation': 'SYNTH'}]
        with self.assertRaises(ValidationError): p.translate_claims(self.plan, self.batch, raw)

    def test_authentic_irrelevant_quote_keeps_synth_negative_judgment(self):
        # This defines the expected semantic negative. Literal matching alone
        # cannot discover irrelevance; the future verifier must make that judgment.
        raw = claims_raw(self.plan, self.batch, 'INDETERMINE')
        for claim in raw['claims']:
            claim['reason'] = 'SYNTH citation authentique mais sans rapport avec cette attribution.'
        result, _ = p.translate_claims(self.plan, self.batch, raw)
        outcome = p.aggregate(self.plan, {self.batch['batch_id']: {'state': 'accepted', 'result': result}})
        self.assertEqual(outcome['documentary'], 'not_supported')

    def test_supported_limitation_without_support_still_refuses(self):
        raw = claims_raw(self.plan, self.batch)
        raw['claims'][0].update(nature='limitation', support=[])
        with self.assertRaisesRegex(ValidationError, 'supported_claim_without_evidence'):
            p.translate_claims(self.plan, self.batch, raw)

    def test_open_correction_remains_unfavorable(self):
        raw = claims_raw(self.plan, self.batch); raw['claims'][0]['action'] = 'corriger'
        result, _ = p.translate_claims(self.plan, self.batch, raw)
        self.assertEqual(result['claims'][0]['action'], 'corriger')
        self.assertEqual(p.aggregate(self.plan, {self.batch['batch_id']: {'state': 'accepted', 'result': result}})['documentary'], 'not_supported')

    def test_action_resolution_cannot_borrow_source_quote(self):
        raw = claims_raw(self.plan, self.batch)
        aid = raw['claims'][0]['assertion_id']; original = next(x for x in self.plan['assertions'] if x['assertion_id']==aid)
        raw['claims'][0].update(action='nuancer', action_resolution={'status':'already_present',
            'location': original['location'], 'quote': self.plan['registry']['segments'][0]['text'], 'reason':'SYNTH'})
        with self.assertRaisesRegex(ValidationError, 'action_resolution_not_in_exact_claim'):
            p.translate_claims(self.plan, self.batch, raw)

    def test_consistent_duplicate_keeps_every_reason_limit_and_occurrence(self):
        raw = claims_raw(self.plan, self.batch); duplicate = deepcopy(raw['claims'][0])
        duplicate.update(reason='Autre raison SYNTH', limits=['Autre réserve SYNTH']); raw['claims'].append(duplicate)
        result, mapping = p.translate_claims(self.plan, self.batch, raw)
        self.assertEqual(len(result['claims']), len(self.batch['assertion_ids']))
        self.assertIn('Autre raison', result['claims'][0]['reason'])
        self.assertIn('Autre réserve SYNTH', result['claims'][0]['limits'])
        self.assertEqual(len(mapping['occurrences'][duplicate['assertion_id']]), 2)

    def test_conflicting_duplicate_and_missing_or_foreign_assertion_refuse(self):
        raw = claims_raw(self.plan, self.batch); duplicate = deepcopy(raw['claims'][0]); duplicate['verdict']='CONTREDIT'
        raw['claims'].append(duplicate)
        with self.assertRaisesRegex(ValidationError, 'contradictory_duplicate'): p.translate_claims(self.plan, self.batch, raw)
        raw = claims_raw(self.plan, self.batch); raw['claims'].pop()
        with self.assertRaisesRegex(ValidationError, 'incomplete_claim_coverage'): p.translate_claims(self.plan, self.batch, raw)
        for value in (True, 0, 'assert-other'):
            raw = claims_raw(self.plan, self.batch); raw['claims'][0]['assertion_id']=value
            with self.assertRaises(ValidationError): p.translate_claims(self.plan, self.batch, raw)

    def test_aggregation_distinguishes_all_states_and_missing(self):
        for state in ('prepared','reserved','launched','returned','partial','refused','interrupted','ambiguous'):
            outcome=p.aggregate(self.plan,{self.batch['batch_id']:{'state':state}})
            self.assertEqual(outcome['execution'],'incomplete'); self.assertEqual(outcome['diffusion'],'ineligible')
        self.assertEqual(p.aggregate(self.plan,{})['execution'],'incomplete')

    def test_aggregation_refuses_old_snapshot_and_policy_without_rebinding(self):
        result,_=p.translate_claims(self.plan,self.batch,claims_raw(self.plan,self.batch))
        for field,value in [('fingerprint','b'*64),('contract','dv.verification/1.0.0')]:
            bad=deepcopy(result); bad[field]=value
            with self.assertRaisesRegex(ValidationError,'aggregation_result_binding'):
                p.aggregate(self.plan,{self.batch['batch_id']:{'state':'accepted','result':bad}})

    def test_aggregation_order_variable_and_metadata_provenance(self):
        plan=plan_fixture(limits=replace(p.Limits(),max_claims=2))
        states={}
        for batch in reversed(plan['batches']):
            if batch['stage']=='claims':
                result,_=p.translate_claims(plan,batch,claims_raw(plan,batch))
                states[batch['batch_id']]={'state':'accepted','result':result}
        outcome=p.aggregate(plan,states)
        self.assertEqual(outcome['execution'],'complete')
        self.assertEqual([c['id'] for c in outcome['result']['claims']], [c['id'] for c in plan['snapshot']['claims']])
        self.assertEqual(outcome['diffusion'],'not_authorized')

    def test_evidence_exact_selection_source_identity_and_partial_reading(self):
        batch=next(b for b in self.plan['batches'] if b['stage']=='evidence')
        raw=evidence_raw(self.plan,batch)
        result,mapping=p.translate_evidence(self.plan,batch,raw)
        self.assertEqual(result['sources'][0]['locators'],['SYNTH p1'])
        self.assertTrue(mapping)
        bad=deepcopy(raw); bad['sources'][0]['version_id']='SYNTH-v2'
        with self.assertRaises(ValidationError): p.translate_evidence(self.plan,batch,bad)
        with self.assertRaisesRegex(ValidationError,'incomplete_source_reading'): p.merge_evidence(self.plan,{})
        self.assertEqual(len(p.merge_evidence(self.plan,{batch['batch_id']:result})['sources']),1)

    def test_source_reuse_identity_binds_context_revision_order_and_policy(self):
        original=p.source_read_identity(self.plan['snapshot'])
        for change in ('version','context','rights'):
            bad=deepcopy(self.plan['snapshot'])
            if change=='version': bad['sources'][0]['reference']['version_id']='SYNTH-v2'
            if change=='context': bad['sources'][0]['passages'][0]['context']='Autre condition SYNTH'
            if change=='rights': bad['sources'][0]['rights']='unknown'
            self.assertNotEqual(original,p.source_read_identity(bad))

    def test_documented_schema_enum_and_string_boundaries(self):
        self.assertEqual(p.schema_capacity({'type':'string','enum':['x']*1000})['total_enum_values'],1000)
        with self.assertRaisesRegex(ValidationError,'capacity_limit'):
            p.schema_capacity({'type':'string','enum':['x']*1001})
        strings=['x'*59]*250+['x'*250]
        self.assertEqual(sum(map(len,strings)),15000)
        p.schema_capacity({'type':'string','enum':strings})
        with self.assertRaisesRegex(ValidationError,'large_enum_string_limit'):
            p.schema_capacity({'type':'string','enum':strings+['x']})
        # <=250 entries uses the total schema string limit, not the 15000
        # additional rule applying to an individual larger enum.
        p.schema_capacity({'type':'string','enum':['x'*120000]})
        with self.assertRaisesRegex(ValidationError,'capacity_limit'):
            p.schema_capacity({'type':'string','enum':['x'*120001]})

    def test_documented_schema_property_and_depth_boundaries(self):
        schema={'type':'object','properties':{str(i):{'type':'string'} for i in range(5000)}}
        self.assertEqual(p.schema_capacity(schema)['total_properties'],5000)
        schema['properties']['one-more']={'type':'string'}
        with self.assertRaisesRegex(ValidationError,'capacity_limit'): p.schema_capacity(schema)
        nested={'type':'string'}
        for _ in range(10): nested={'type':'array','items':nested}
        self.assertEqual(p.schema_capacity(nested)['maximum_nesting'],10)
        with self.assertRaisesRegex(ValidationError,'capacity_limit'):
            p.schema_capacity({'type':'array','items':nested})

    def test_reused_schema_defs_count_large_segment_enum_once_and_expand_depth(self):
        packet=p.packet_for(self.plan,self.batch)
        schema=p.response_schema('claims',packet)
        metric=p.schema_capacity(schema)
        self.assertLess(metric['total_enum_values'],1000)
        self.assertEqual(schema['$defs']['segment_id']['enum'],[x['segment_id'] for x in packet['segments']])
        self.assertLessEqual(metric['maximum_nesting'],10)

    def test_malformed_judgment_types_refuse_before_duplicate_folding(self):
        for field,value in [('reason',True),('limits',True),('verdict',[]),('nature',[]),('action',[])]:
            raw=claims_raw(self.plan,self.batch); raw['claims'][0][field]=value
            raw['claims'].append(deepcopy(raw['claims'][0]))
            with self.subTest(field=field),self.assertRaises(ValidationError):
                p.translate_claims(self.plan,self.batch,raw)


def documentary_fixture():
    """SYNTH href deliberately absent from the normalized passage."""
    import hashlib
    original = '<html>SYNTH 🙂<a href="/SYNTH/person/Élise125">SYNTH Élise</a></html>'.encode('utf-8')
    digest = hashlib.sha256(original).hexdigest()
    snapshot = snapshot_fixture()
    snapshot['sources'][0]['reference']['sha256'] = digest
    snapshot['sources'][0]['latest_sha256'] = digest
    snapshot['content']['source_snapshot'][0]['sha256'] = digest
    snapshot['claims'] = list(v._scalars(snapshot['content']))
    text = original.decode('utf-8'); start = text.index('<a '); end = text.index('</a>') + 4
    proof = {'kind': 'pinned_original_segment', 'source_version': deepcopy(snapshot['sources'][0]['reference']),
             'original_sha256': digest, 'start': start, 'end': end, 'offset_unit': p.OFFSET_UNIT,
             'exact_segment': text[start:end]}
    record = {'family': 'derived_relation', 'purpose': 'SYNTH lien nominatif et namespace local déclarés.',
              'locations': ['/public_payload/vote/person'], 'proof': proof}
    bundle = {'contract': p.DOCUMENTARY_INPUT, 'fingerprint': sha256_digest(snapshot), 'records': [record]}
    return snapshot, bundle, {digest: original}


class DocumentaryInputTests(SimpleTestCase):
    def setUp(self):
        self.snapshot, self.bundle, self.originals = documentary_fixture()
        self.annex = p.freeze_documentary_input(self.snapshot, self.bundle, source_originals=self.originals)

    def test_exact_original_href_is_in_claims_prompt_with_binding_but_not_reading_stage(self):
        baseline = plan_fixture(self.snapshot)
        plan = plan_fixture(self.snapshot, documentary_annex=self.annex)
        self.assertNotEqual(plan['plan_digest'], baseline['plan_digest'])
        self.assertEqual(plan['registry'], baseline['registry'])
        self.assertEqual(plan['assertions'], baseline['assertions'])
        self.assertEqual(plan['source_read_identity'], baseline['source_read_identity'])
        evidence = next(b for b in plan['batches'] if b['stage'] == 'evidence')
        claims = next(b for b in plan['batches'] if b['stage'] == 'claims')
        self.assertNotIn('documentary_evidence', p.packet_for(plan, evidence))
        self.assertEqual(evidence['measurements'], baseline['batches'][0]['measurements'])
        packet = p.packet_for(plan, claims)
        self.assertEqual(packet['documentary_evidence'], self.annex)
        self.assertIn('/SYNTH/person/Élise125', p.prompt_for('claims', packet))
        self.assertNotIn('/SYNTH/person/Élise125', packet['sources'][0]['passages'][0]['exact_text'])
        self.assertGreater(claims['measurements']['input_bytes'], next(b for b in baseline['batches'] if b['stage']=='claims')['measurements']['input_bytes'])
        p.validate_plan(plan)

    def test_pinned_original_bytes_offsets_versions_and_truncation_refuse(self):
        changes = [('end', True), ('start', -1), ('end', 10000), ('exact_segment', 'SYNTH invented'),
                   ('offset_unit', 'utf8_bytes'), ('original_sha256', 'a'*64)]
        for field, value in changes:
            bad = deepcopy(self.bundle); bad['records'][0]['proof'][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)
        for originals in ({}, {next(iter(self.originals)): b'SYNTH wrong bytes'}):
            with self.assertRaises(ValidationError):
                p.freeze_documentary_input(self.snapshot, self.bundle, source_originals=originals)
        bad = deepcopy(self.bundle); bad['records'][0]['proof']['source_version']['version_id'] = 'SYNTH-new'
        with self.assertRaises(ValidationError):
            p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)

    def test_snapshot_pointer_typed_state_remains_declared_context_and_not_verdict(self):
        record = {'family': 'application_state', 'purpose': 'SYNTH déclaration à contrôler humainement.',
            'locations': ['/title'], 'proof': {'kind': 'snapshot_pointer', 'pointer': '/title',
                                             'expected': self.snapshot['content']['title']}}
        bundle = deepcopy(self.bundle); bundle['records'] = [record]
        annex = p.freeze_documentary_input(self.snapshot, bundle)
        self.assertIn('separate_deterministic_or_human_control', annex['records'][0]['control'])
        self.assertEqual(annex['favorable_verdict'], 'not_produced')
        self.assertEqual(annex['policy_effect'], 'none')
        bundle['records'][0]['proof']['expected'] = 'SYNTH different current state'
        with self.assertRaises(ValidationError): p.freeze_documentary_input(self.snapshot, bundle)

    def test_external_admin_artifact_and_foreign_snapshot_never_enter_model(self):
        bundle = deepcopy(self.bundle); bundle['records'][0]['proof'] = {
            'kind': 'bound_artifact', 'path': '../SYNTH-private.json', 'sha256': 'a'*64,
            'observation': 'SYNTH private artifact'}
        with self.assertRaisesRegex(ValidationError, 'separate_human_control'):
            p.freeze_documentary_input(self.snapshot, bundle)
        snapshot = deepcopy(self.snapshot); snapshot['revision'] += 1
        with self.assertRaises(ValidationError): p.validate_documentary_input(snapshot, self.annex)
        with self.assertRaises(ValidationError): p.freeze_documentary_input(snapshot, self.bundle, source_originals=self.originals)

    def test_record_digest_paths_capture_bounds_duplicates_and_unknown_fields_refuse(self):
        for field, value in [('locations', ['/SYNTH-missing']), ('family', 'SYNTH-unknown'),
                             ('family', []), ('purpose', True)]:
            bad = deepcopy(self.bundle); bad['records'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)
        bad = deepcopy(self.bundle); bad['records'][0]['qualification'] = 'SYNTH favorable'
        with self.assertRaises(ValidationError): p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)
        bad = deepcopy(self.bundle); bad['records'].append(deepcopy(bad['records'][0]))
        with self.assertRaisesRegex(ValidationError, 'record_digest'):
            p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)
        bad = deepcopy(self.bundle); bad['records'][0]['purpose'] = 'SYNTH'*p.MAX_DOCUMENTARY_INPUT_BYTES
        with self.assertRaisesRegex(ValidationError, 'capture_limit'):
            p.freeze_documentary_input(self.snapshot, bad, source_originals=self.originals)
        bad = deepcopy(self.annex); bad['records'][0]['proof']['exact_segment'] += 'SYNTH'
        with self.assertRaises(ValidationError): p.validate_documentary_input(self.snapshot, bad)

    def test_documentary_context_does_not_create_support_or_exempt_limitations(self):
        plan = plan_fixture(self.snapshot, documentary_annex=self.annex)
        batch = next(b for b in plan['batches'] if b['stage'] == 'claims')
        raw = claims_raw(plan, batch)
        identifier = self.annex['records'][0]['record_id']; raw['claims'][0]['reason'] = 'SYNTH contexte '+identifier
        result, trace = p.translate_claims(plan, batch, raw)
        self.assertEqual(trace['documentary_annex_digest'], self.annex['annex_digest'])
        self.assertEqual(trace['documentary_mentions'][0]['record_ids'], [identifier])
        self.assertEqual(result['claims'][0]['reason'], raw['claims'][0]['reason'])
        self.assertEqual(result['contract'], v.POLICY)
        bad = deepcopy(raw); bad['claims'][0]['support'][0]['segment_id'] = identifier
        with self.assertRaisesRegex(ValidationError, 'segment_reference'): p.translate_claims(plan, batch, bad)
        bad = deepcopy(raw); bad['claims'][0]['nature'] = 'limitation'; bad['claims'][0]['support'] = []
        with self.assertRaises(ValidationError): p.translate_claims(plan, batch, bad)
