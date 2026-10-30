# Copie publique : les groupes nommés dans les fixtures sont fictifs.
"""SYNTH proof routing/adversarial contract tests; no provider or real DB."""
from copy import deepcopy
from datetime import date
from unittest.mock import patch
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from atelier.services.canonical import sha256_digest
from atelier.services import verification as v
from atelier.services import proof_classification as c
from atelier.services import verification_preflight as f
from atelier.services import verification_counterevidence as x
from atelier.services import verification_layered_plan as lp
from atelier.services import verification_protocol as p
from atelier.tests.test_verification_protocol import snapshot_fixture, claims_raw


def rich_fixture():
    snapshot = snapshot_fixture()
    ref = deepcopy(snapshot['sources'][0]['reference'])
    passage = snapshot['sources'][0]['passages'][0]
    payload = snapshot['content']['public_payload']
    payload.update({'evidence': {'source_version': ref, 'exact_quote': passage['exact_text'], 'locator': passage['locator']},
        'acts': [{'scrutiny_id': 'SYNTH-VOTE', 'included': True}],
        'actors': {'scope': 'personal', 'speaker': {'person_id': 'SYNTH-P1'}, 'compared_actor': {'person_id': 'SYNTH-P1'}},
        'catalogue': {'scrutinies': [{'scrutin_id': 'SYNTH-VOTE', 'official_title': 'SYNTH vote',
            'procedure': 'SYNTH ensemble', 'submitted_text_id': 'SYNTH-TEXT', 'text_reference_role': 'submitted'}],
            'text_versions': [{'id': 'SYNTH-TEXT', 'source_version': ref}],
            'vote_observations': [{'scrutiny_id': 'SYNTH-VOTE', 'person_id': 'SYNTH-P1', 'source_version': ref}],
            'persons': [{'id': 'SYNTH-P1', 'display_name': 'SYNTH Élise'}], 'organizations': [], 'affiliations': []}})
    snapshot['claims'] = list(v._scalars(snapshot['content']))
    return snapshot


def rights_fixture(snapshot, status='compatible', expires_on=None):
    value = {'contract': f.RIGHTS, 'fingerprint': sha256_digest(snapshot), 'render_mode': 'private_review',
        'usages': [{'source_id': s['reference']['source_id'], 'version_id': s['reference']['version_id'],
            'source_sha256': s['reference']['sha256'], 'usage': 'private_review', 'status': status,
            'qualification_reference': 'SYNTH usage only', 'expires_on': expires_on} for s in snapshot['sources']]}
    value['rights_digest'] = sha256_digest(value)
    return value


def editorial_rule(location='/public_payload/acts/0/included'):
    return {'selection_rule_id': 'SYNTH-selection', 'version': '1', 'location': location,
        'motif': 'SYNTH objet retenu dans le périmètre.', 'included_objects': ['SYNTH-VOTE'],
        'excluded_objects': ['SYNTH-OTHER'], 'limits': ['SYNTH aucun fait institutionnel déduit.'],
        'counter_cases': ['SYNTH objet comparable exclu vérifié.']}


class ClassificationTests(SimpleTestCase):
    def test_political_theme_labels_and_official_ids_cannot_be_editorial_exemptions(self):
        for origin, field in (('proposed', 'label'), ('official', 'label'), ('official', 'id'), ('proposed', 'selection_reason')):
            snapshot = rich_fixture()
            snapshot['content']['public_payload']['themes'] = [{'id': 'SYNTH:THEME',
                'label': 'SYNTH tous les élus ont trahi.', 'origin': origin, 'target_type': 'text',
                'target_id': 'SYNTH-TEXT', 'selection_reason': 'SYNTH accusation politique.'}]
            snapshot['claims'] = list(v._scalars(snapshot['content']))
            rule = editorial_rule('/public_payload/themes/0/' + field)
            with self.assertRaises(ValidationError):
                c.classify_snapshot(snapshot, editorial_rules=[rule])

    def test_only_proposed_theme_identifier_for_exact_received_target_is_selection(self):
        snapshot = rich_fixture()
        snapshot['content']['public_payload']['themes'] = [{'id': 'SYNTH:THEME', 'label': 'SYNTH proposition.',
            'origin': 'proposed', 'target_type': 'text', 'target_id': 'SYNTH-TEXT'}]
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        rule = editorial_rule('/public_payload/themes/0/id'); rule['included_objects'] = ['SYNTH-TEXT']
        report = c.classify_snapshot(snapshot, editorial_rules=[rule])
        self.assertEqual(next(row for row in report['claims'] if row['location'] == '/public_payload/themes/0/id')['layer'], 'editorial')
        self.assertEqual(next(row for row in report['claims'] if row['location'] == '/public_payload/themes/0/label')['layer'], 'semantic')
        c.validate_classification(snapshot, report)
        snapshot['content']['public_payload']['themes'][0]['target_id'] = 'SYNTH-NOT-RECEIVED'
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        with self.assertRaisesRegex(ValidationError, 'target_not_exact_received_object'):
            c.classify_snapshot(snapshot, editorial_rules=[rule])

    def test_default_semantic_and_editorial_selection_not_institutional_fact(self):
        snapshot = rich_fixture()
        report = c.classify_snapshot(snapshot, editorial_rules=[editorial_rule()])
        self.assertEqual(next(r for r in report['claims'] if r['location'] == '/short_answer')['layer'], 'semantic')
        selected = next(r for r in report['claims'] if r['location'].endswith('/included'))
        self.assertEqual(selected['layer'], 'editorial')
        self.assertEqual(selected['fact_type'], 'editorial_selection')
        c.validate_classification(snapshot, report)

    def test_political_date_person_vote_cannot_be_editorial(self):
        snapshot = rich_fixture()
        for location in ('/public_payload/vote/date', '/public_payload/vote/person', '/short_answer'):
            with self.subTest(location=location), self.assertRaisesRegex(ValidationError, 'external_fact'):
                c.classify_snapshot(snapshot, editorial_rules=[editorial_rule(location)])

    def test_unknown_is_explicit_and_never_proves_absence(self):
        snapshot = rich_fixture()
        snapshot['content']['public_payload']['membership_end'] = None
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        annotation = {'location': '/public_payload/membership_end', 'unknown_kind': 'membership_end_unknown',
            'motif': 'SYNTH fin non établie.', 'limits': ['SYNTH observation seule.']}
        report = c.classify_snapshot(snapshot, unknowns=[annotation])
        self.assertFalse(report['unknowns'][0]['absence_proven'])
        self.assertIsNone(report['unknowns'][0]['represented_value'])
        c.validate_classification(snapshot, report)

    def test_real_date_cannot_be_exempted_as_unknown(self):
        snapshot = rich_fixture()
        annotation = {'location': '/public_payload/vote/date', 'unknown_kind': 'membership_end_unknown',
            'motif': 'SYNTH', 'limits': ['SYNTH']}
        with self.assertRaisesRegex(ValidationError, 'known_external_fact'):
            c.classify_snapshot(snapshot, unknowns=[annotation])

    def test_forged_routing_digest_does_not_make_semantic_external_fact_editorial(self):
        snapshot = rich_fixture()
        report = c.classify_snapshot(snapshot)
        report['claims'][0]['layer'] = 'editorial'
        report.pop('classification_digest'); report['classification_digest'] = sha256_digest(report)
        with self.assertRaisesRegex(ValidationError, 'classification_binding'):
            c.validate_classification(snapshot, report)

    def test_certificate_validator_is_required_not_only_proven_label(self):
        snapshot = rich_fixture()
        with patch('atelier.services.provenance.validate_report', side_effect=ValidationError('SYNTH-forged-original')) as validator:
            with self.assertRaisesRegex(ValidationError, 'forged-original'):
                c.classify_snapshot(snapshot, provenance_report={'certificates': [{'status': 'proven'}]})
        self.assertEqual(validator.call_count, 1)


class PreflightTests(SimpleTestCase):
    def report(self, snapshot=None, rights_status='compatible', classification=None):
        snapshot = snapshot or rich_fixture()
        classification = classification or c.classify_snapshot(snapshot)
        # SYNTH seam isolates preflight controls. Real provenance recomputation
        # is mandatory in classification and has its own originals-based tests.
        with patch.object(f, '_proven', return_value=True):
            return f.build_preflight(snapshot, classification, rights_report=rights_fixture(snapshot, rights_status), as_of=date(2000, 1, 1))

    def test_nine_directive_reasons_and_zero_spent_phases(self):
        report = self.report()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual([check['code'] for check in report['checks']], list(f.REASONS))
        self.assertEqual(report['cost']['actual_phases_reserved'], 0)
        self.assertEqual(report['cost']['actual_phases_launched'], 0)
        self.assertFalse(report['received'])
        self.assertEqual(report['cost']['authorization'], 'NOT_AUTHORIZED')

    def test_essential_null_unknown_blocks_without_inventing_semantic_claim(self):
        snapshot = rich_fixture()
        snapshot['content']['public_payload']['essential_end'] = None
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        unknown = {'location': '/public_payload/essential_end', 'unknown_kind': 'membership_end_unknown',
                   'motif': 'SYNTH borne nécessaire inconnue.', 'limits': ['SYNTH aucune sortie déduite.']}
        classification = c.classify_snapshot(snapshot, unknowns=[unknown])
        self.assertFalse(any(row['location'] == unknown['location'] for row in classification['claims']))
        report = self.report(snapshot, classification=classification)
        self.assertEqual(report['status'], 'hold')
        check = next(row for row in report['checks'] if row['code'] == f.REASONS[7])
        self.assertEqual(check['status'], 'hold')
        self.assertIn(unknown['location'], check['locations'])
        self.assertEqual(report['cost']['unknown_assertions'], 1)

    def test_received_historical_ready_does_not_authorize_current_expired_rights(self):
        snapshot = rich_fixture(); classification = c.classify_snapshot(snapshot)
        with patch.object(f, '_proven', return_value=True):
            report = f.build_preflight(snapshot, classification,
                rights_report=rights_fixture(snapshot, expires_on='2000-01-02'), as_of=date(2000, 1, 1))
            self.assertEqual(report['status'], 'ready')
            artifact = SimpleNamespace(kind='preflight', revision_fingerprint=report['fingerprint'],
                payload_digest=sha256_digest(report), payload=report)
            f.validate_preflight(snapshot, classification, report)
            with self.assertRaisesRegex(ValidationError, 'current_rights_unqualified_or_expired'):
                f.require_received_preflight(snapshot, classification, report, artifact)

    def test_collective_event_link_uses_group_proof_not_speaker_personal_vote(self):
        snapshot = rich_fixture()
        actors = snapshot['content']['public_payload']['actors']
        actors['scope'] = 'collective'
        actors['compared_actor'] = {'organization_id': 'SYNTH-G1'}
        positions = snapshot['content']['public_payload']['catalogue']['vote_observations']
        positions[0]['affiliation_at_event_id'] = 'SYNTH-G1'
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        self.assertEqual(self.report(snapshot)['status'], 'ready')
        positions[0]['affiliation_at_event_id'] = 'SYNTH-G2'
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        report = self.report(snapshot)
        self.assertEqual(report['status'], 'hold')
        self.assertEqual(next(c for c in report['checks'] if c['code'] == f.REASONS[4])['status'], 'hold')

    def test_each_missing_chain_reason_prevents_ready(self):
        changes = [
            (f.REASONS[0], lambda s: s['sources'][0].update(state='missing')),
            (f.REASONS[1], lambda s: s['content']['public_payload']['evidence'].update(exact_quote='SYNTH invented')),
            (f.REASONS[2], lambda s: s['content']['public_payload']['acts'][0].update(scrutiny_id='SYNTH absent')),
            (f.REASONS[3], lambda s: s['content']['public_payload']['catalogue']['scrutinies'][0].update(procedure='')),
            (f.REASONS[4], lambda s: s['content']['public_payload']['catalogue'].update(vote_observations=[])),
            (f.REASONS[5], lambda s: s['content']['public_payload']['catalogue']['scrutinies'][0].update(submitted_text_id=None)),
            (f.REASONS[6], lambda s: s['content']['public_payload']['actors']['compared_actor'].update(person_id='SYNTH-P2')),
        ]
        for reason, change in changes:
            snapshot = rich_fixture(); change(snapshot); snapshot['claims'] = list(v._scalars(snapshot['content']))
            report = self.report(snapshot)
            with self.subTest(reason=reason):
                self.assertNotEqual(report['status'], 'ready')
                self.assertNotEqual(next(c for c in report['checks'] if c['code'] == reason)['status'], 'pass')

    def test_unknown_bound_and_incompatible_rights_block(self):
        snapshot = rich_fixture()
        snapshot['content']['public_payload']['catalogue']['affiliations'] = [{'person_id': 'SYNTH-P1', 'starts_on': '2000-01-01', 'ends_on': '2000-01-01', 'observed_on': '2000-01-01'}]
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        report = self.report(snapshot)
        self.assertEqual(next(c for c in report['checks'] if c['code'] == f.REASONS[7])['status'], 'hold')
        self.assertEqual(self.report(rights_status='incompatible')['status'], 'rejected')

    def test_unqualified_or_expired_rights_are_not_general_authority(self):
        snapshot = rich_fixture(); classification = c.classify_snapshot(snapshot)
        with patch.object(f, '_proven', return_value=True):
            for rights in (None, rights_fixture(snapshot, 'unqualified'), rights_fixture(snapshot, expires_on='1999-12-31')):
                report = f.build_preflight(snapshot, classification, rights_report=rights, as_of=date(2000, 1, 1))
                self.assertEqual(report['status'], 'hold')

    def test_false_source_reuse_label_does_not_reduce_cost(self):
        snapshot = rich_fixture(); classification = c.classify_snapshot(snapshot)
        receipt = {'source_read_identity': p.source_read_identity(snapshot), 'origin': 'provider_response_file', 'state': 'accepted', 'source_phases': 1}
        plain = f.estimate_cost(snapshot, classification)
        forged = f.estimate_cost(snapshot, classification, reuse_receipts=[receipt])
        self.assertEqual(forged['minimum_model_phases'], plain['minimum_model_phases'])
        self.assertEqual(forged['admissible_reuses'], [])

    def test_trusted_runtime_reuse_is_not_an_independent_read(self):
        snapshot = rich_fixture(); classification = c.classify_snapshot(snapshot)
        receipt = {'source_read_identity': p.source_read_identity(snapshot), 'origin': 'provider_response_file', 'state': 'accepted', 'source_phases': 1}
        cost = f.estimate_cost(snapshot, classification, reuse_receipts=[receipt], reuse_validator=lambda value: value is receipt)
        self.assertEqual(cost['reused_source_phases'], 1)
        self.assertFalse(cost['admissible_reuses'][0]['independent_read'])

    def test_rights_fingerprint_tamper_even_with_recomputed_digest_refuses(self):
        snapshot = rich_fixture(); classification = c.classify_snapshot(snapshot)
        rights = rights_fixture(snapshot); rights['fingerprint'] = 'b' * 64
        rights.pop('rights_digest'); rights['rights_digest'] = sha256_digest(rights)
        with self.assertRaisesRegex(ValidationError, 'rights_binding'):
            f.build_preflight(snapshot, classification, rights_report=rights, as_of=date(2000, 1, 1))


class CounterevidenceTests(SimpleTestCase):
    def result(self, snapshot, location, quote, verdict='PARTIEL', against=None):
        claim = next(c for c in snapshot['claims'] if c['location'] == location)
        return {'fingerprint': sha256_digest(snapshot), 'claims': [{**deepcopy(claim), 'verdict': verdict,
            'support': [{'passage_id': 31, 'quote': quote}], 'against': against or []}]}

    def test_authentic_a3_irrelevant_selected_support_is_negative_witness(self):
        snapshot = rich_fixture(); payload = snapshot['content']['public_payload']
        payload['catalogue']['affiliations'] = [{'person_id': 'SYNTH-P1', 'organization_id': 'SYNTH:GROUP:SYNTHGROUP_A',
            'ends_on': '2000-01-01', 'observed_on': '2000-01-01', 'status': 'membership_attested_at_scrutiny'}]
        payload['catalogue']['organizations'] = [{'id': 'SYNTH:GROUP:SYNTHGROUP_A', 'name': 'SYNTHGROUP_A', 'kind': 'parliamentary_group'}]
        snapshot['sources'][0]['passages'][0]['exact_text'] = 'SYNTHGROUP_B, SYNTHGROUP_C, SYNTHGROUP_D, début SYNTHGROUP_E. Puis SYNTHGROUP_A : SYNTH Élise.'
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        result = self.result(snapshot, '/public_payload/catalogue/affiliations/0/ends_on', 'SYNTHGROUP_B, SYNTHGROUP_C, SYNTHGROUP_D, début SYNTHGROUP_E.')
        before = deepcopy(result)
        report = x.scan_counterevidence(snapshot, result)
        codes = {f['code'] for f in report['findings']}
        self.assertIn('expected_entity_not_in_selected_support', codes)
        self.assertIn('observation_is_not_membership_boundary', codes)
        self.assertFalse(report['summary']['counterevidence_complete'])
        self.assertEqual(result, before)
        self.assertEqual(result['claims'][0]['verdict'], 'PARTIEL')
        x.validate_counterevidence(snapshot, report, result)

    def test_negation_condition_outside_exact_quote_flags_without_replacing_quote(self):
        for text, quote in (("SYNTH elle n'est pas favorable.", 'favorable'), ('SYNTH elle votera pour si le texte est amendé.', 'elle votera pour')):
            snapshot = rich_fixture(); snapshot['sources'][0]['passages'][0]['exact_text'] = text
            result = self.result(snapshot, '/short_answer', quote)
            report = x.scan_counterevidence(snapshot, result)
            self.assertIn('negation_or_condition_outside_quote', {f['code'] for f in report['findings']})
            self.assertEqual(result['claims'][0]['support'][0]['quote'], quote)

    def test_good_other_support_does_not_hide_bad_selected_fragment(self):
        snapshot = rich_fixture(); payload = snapshot['content']['public_payload']
        payload['catalogue']['affiliations'] = [{'person_id': 'SYNTH-P1', 'organization_id': 'SYNTH:GROUP:SYNTHGROUP_A',
            'ends_on': '2000-01-01', 'observed_on': '2000-01-01'}]
        payload['catalogue']['organizations'] = [{'id': 'SYNTH:GROUP:SYNTHGROUP_A', 'name': 'SYNTHGROUP_A'}]
        good = 'SYNTHGROUP_A : SYNTH Élise.'; bad = 'SYNTHGROUP_B, SYNTHGROUP_C, SYNTHGROUP_D, début SYNTHGROUP_E.'
        snapshot['sources'][0]['passages'][0]['exact_text'] = good + ' ' + bad
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        result = self.result(snapshot, '/public_payload/catalogue/affiliations/0/ends_on', bad)
        result['claims'][0]['support'].insert(0, {'passage_id': 31, 'quote': good})
        report = x.scan_counterevidence(snapshot, result)
        flags = [f for f in report['findings'] if f['code'] == 'expected_entity_not_in_selected_support']
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['proof']['selected_passages'][0]['quote'], bad)

    def test_counterproof_context_only_and_wrong_quote_are_distinct(self):
        snapshot = rich_fixture(); snapshot['sources'][0]['passages'][0]['exact_text'] = 'SYNTH en-tête. SYNTH le vote est contre.'
        result = self.result(snapshot, '/short_answer', 'SYNTH en-tête.', against=[{'passage_id':31,'quote':'SYNTH le vote est contre.'}])
        report = x.scan_counterevidence(snapshot, result)
        self.assertEqual({'context_only_evidence', 'received_counterevidence'}, {f['code'] for f in report['findings']})
        result['claims'][0]['against'][0]['quote'] = 'SYNTH invented'
        with self.assertRaisesRegex(ValidationError, 'quote_not_in_received'):
            x.scan_counterevidence(snapshot, result)

    def test_actor_group_government_and_procedural_scope_guards(self):
        snapshot = rich_fixture(); payload = snapshot['content']['public_payload']
        payload['actors'].update(scope='government'); payload['compared_actor_scope']='personal'
        payload['catalogue']['scrutinies'][0].update(procedure='motion de rejet préalable')
        payload['conclusion_object_scope']='whole_text'
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        report=x.scan_counterevidence(snapshot)
        self.assertIn('government_is_not_minister_personal_vote',{f['code'] for f in report['findings']})
        self.assertIn('procedural_vote_is_not_whole_text',{f['code'] for f in report['findings']})
        payload['actors'].update(scope='party', represented_entity={'organization_id':'SYNTH-G','kind':'party'})
        payload['catalogue']['organizations']=[{'id':'SYNTH-G','kind':'parliamentary_group'}]
        snapshot['claims']=list(v._scalars(snapshot['content']))
        self.assertIn('parliamentary_group_is_not_party',{f['code'] for f in x.scan_counterevidence(snapshot)['findings']})

    def test_collective_group_is_not_unanimity_and_no_finding_is_not_absence_proof(self):
        snapshot=rich_fixture(); snapshot['content']['short_answer']='SYNTH unanimité du groupe.'
        snapshot['content']['public_payload']['actors']['scope']='collective'
        snapshot['claims']=list(v._scalars(snapshot['content']))
        report=x.scan_counterevidence(snapshot)
        self.assertIn('collective_group_does_not_prove_unanimity',{f['code'] for f in report['findings']})
        empty=x.scan_counterevidence(rich_fixture())
        self.assertTrue(empty['summary']['no_result_is_not_absence_proof'])
        self.assertFalse(empty['summary']['counterevidence_complete'])
        self.assertEqual({a['axis'] for a in empty['axes']},set(x.AXES))

    def test_declared_distinct_labels_never_replace_runtime_identity_separation(self):
        snapshot=rich_fixture()
        for generation, first, counter in ((None,1,2),(1,1,2),(1,2,2),(1,2,3)):
            report=x.scan_counterevidence(snapshot,generation_actor_id=generation,
                first_judgment_actor_id=first,counter_actor_id=counter,
                first_judgment_execution_id='SYNTH-first',counter_execution_id='SYNTH-counter')
            self.assertFalse(report['identity']['runtime_identity_verified'])
            self.assertFalse(report['summary']['counterevidence_complete'])
        bad=deepcopy(report); bad['summary']['counterevidence_complete']=True
        bad.pop('counterevidence_digest');bad['counterevidence_digest']=sha256_digest(bad)
        with self.assertRaisesRegex(ValidationError,'binding_or_findings'):
            x.validate_counterevidence(snapshot,bad)


class LayeredPlanTests(SimpleTestCase):
    def plan(self):
        snapshot=rich_fixture(); classification=c.classify_snapshot(snapshot,editorial_rules=[editorial_rule()])
        preflight=f.build_preflight(snapshot,classification,as_of=date(2000,1,1))
        return lp.build_plan(snapshot,classification=classification,preflight=preflight,
            job_id=1,job_key='SYNTH-job',revision_id=1,owner_id=1,model='SYNTH',effort='none')

    def states(self,plan,verdict='ETAYE_DANS_PERIMETRE'):
        states={}
        for batch in plan['batches']:
            if batch['stage']=='claims':
                raw=claims_raw(plan,batch,verdict)
                translated,_=p.translate_claims(plan,batch,raw)
                states[batch['batch_id']]={'state':'accepted','result':translated}
        return states

    def test_b_only_packets_and_exact_nonsemantic_coverage_full_fingerprint(self):
        plan=self.plan(); lp.validate_plan(plan)
        wanted=set(plan['proof_architecture']['semantic_claim_ids'])
        actual=set()
        for batch in plan['batches']:
            if batch['stage']=='claims':
                packet=lp.packet_for(plan,batch)
                actual.update(a['claim_id'] for a in packet['assertions'])
                self.assertEqual(packet['sources'],plan['snapshot']['sources'])
        self.assertEqual(wanted,actual)
        nonsemantic={c['claim_id'] for c in plan['proof_architecture']['nonsemantic_coverage']}
        self.assertFalse(actual & nonsemantic)
        self.assertEqual(actual|nonsemantic,{c['id'] for c in plan['snapshot']['claims']})
        self.assertEqual(plan['fingerprint'],sha256_digest(plan['snapshot']))
        self.assertEqual(plan['allocation_proposal']['actual_phases_reserved'],0)

    def test_complete_first_pass_is_not_publishable_even_all_synth_semantic_supported(self):
        plan=self.plan(); outcome=lp.aggregate(plan,self.states(plan))
        self.assertEqual(outcome['execution'],'complete')
        self.assertEqual(outcome['documentary'],'insufficient_evidence')
        self.assertEqual(outcome['diffusion'],'not_authorized')
        self.assertEqual(len(outcome['result']['claims']),len(plan['snapshot']['claims']))
        self.assertFalse(outcome['result']['counterevidence_complete'])
        self.assertFalse(outcome['result']['human_reviewed_exact_revision'])
        lp.validate_result(plan,outcome['result'])

    def test_null_unknown_has_separate_d_coverage_without_false_semantic_claim(self):
        snapshot = rich_fixture()
        snapshot['content']['public_payload']['essential_end'] = None
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        classification = c.classify_snapshot(snapshot, unknowns=[{'location': '/public_payload/essential_end',
            'unknown_kind': 'membership_end_unknown', 'motif': 'SYNTH borne nécessaire inconnue.',
            'limits': ['SYNTH pas une preuve absence.']}])
        preflight = f.build_preflight(snapshot, classification, as_of=date(2000, 1, 1))
        plan = lp.build_plan(snapshot, classification=classification, preflight=preflight,
            job_id=1, job_key='SYNTH-job', revision_id=1, owner_id=1, model='SYNTH', effort='none')
        result = lp.aggregate(plan, self.states(plan))['result']
        self.assertEqual(len(result['claims']), len(snapshot['claims']))
        self.assertEqual(result['unknown_fields_count'], 1)
        self.assertEqual(result['unknown_annotations'][0]['represented_value'], None)
        self.assertEqual(result['documentary'], 'not_supported')
        self.assertIn(classification['unknowns'][0]['claim_id'], result['issues'])
        lp.validate_result(plan, result)

    def test_negative_mixed_judgment_stays_negative_and_missing_batch_incomplete(self):
        plan=self.plan(); outcome=lp.aggregate(plan,self.states(plan,'PARTIEL'))
        self.assertEqual(outcome['documentary'],'not_supported')
        self.assertEqual(lp.aggregate(plan,{})['execution'],'incomplete')

    def test_plan_tamper_repartition_or_drop_even_with_digest_refuses(self):
        plan=self.plan();bad=deepcopy(plan)
        batch=next(b for b in bad['batches'] if b['stage']=='claims')
        batch['assertion_ids'].pop();bad.pop('plan_digest');bad['plan_digest']=sha256_digest(bad)
        with self.assertRaisesRegex(ValidationError,'routing_or_digest'):
            lp.validate_plan(bad)

    def test_layered_result_cannot_replace_editorial_decision_with_institutional_proven(self):
        plan=self.plan();result=lp.aggregate(plan,self.states(plan))['result']
        selected=next(r for r in result['claims'] if r['layer']=='editorial');selected['status']='proven'
        result.pop('result_digest');result['result_digest']=sha256_digest(result)
        with self.assertRaisesRegex(ValidationError,'coverage_or_status'):
            lp.validate_result(plan,result)
