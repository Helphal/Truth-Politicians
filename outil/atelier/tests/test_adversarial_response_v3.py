"""Pure SYNTH wire contract tests; no ORM, role invention or provider call."""
from copy import deepcopy
import unittest
from types import ModuleType
from unittest.mock import patch

from django.core.exceptions import ValidationError
from atelier.services import verification_counterevidence as counter
from atelier.services.canonical import sha256_digest

LEGACY_GOLDEN = {
    '1': {'plan_sha256':'28bd9360e61052e07709b118e1c5af7b41194f765ce4eb1177457c1ff011a30e',
          'response_sha256':'edb2b67090d5bb6aa7316b86b0c3283d11bc68bd620f310163c75b5f130b0aad',
          'report_sha256':'ffb182f26b9a23a9dcb8d8a832b05cd1515b3c2ad975c8fe00d0f521f37047b9'},
    '2': {'plan_sha256':'b510cb45ff24d4389d0a73809124e074378bdbeb46451877e06e1250a02bfd73',
          'response_sha256':'77bf7f3e7c7e4a49be19aa05ed5096c3efd3da30635e11de1ae80c8b99e02696',
          'report_sha256':'3fae702a4ca49c7ab40db5f540b7179b21246a323cb7fc48ac90f2a689bf98ac'}}


def fixture(dependency=False):
    quote = 'SYNTH appui reçu intégral ; ponctuation et Unicode inchangés.'
    original = {'id':'SYNTH-claim-1','location':'/short_answer','value':'SYNTH conclusion'}
    snapshot = {'claims':[original], 'content':{'short_answer':'SYNTH conclusion'},
        'scope':'synthetic', 'sources':[{'reference':{'source_id':'SYNTH-source','version_id':'SYNTH-v1',
            'sha256':'a'*64},'passages':[{'id':1,'exact_text':'SYNTH avant. '+quote+' SYNTH après.',
            'locator':'SYNTH codepoints'}]}]}
    graph = {'graph_digest':'SYNTH-graph-digest','counterevidence_requirements':
        {axis:{'required':axis!='affiliation_change'} for axis in counter.AXES}}
    if dependency: snapshot['conclusion']={'archetype':'SYNTH-dependency-fixture'}
    classification = {'classification_digest':'SYNTH-classification-digest','claims':[
        {'claim_id':original['id'],'layer':'semantic','necessary_for_conclusion':True}],
        'dependency_graph':graph}
    first = {'fingerprint':sha256_digest(snapshot),'claims':[{**original,'verdict':'ETAYE_DANS_PERIMETRE',
        'support':[{'passage_id':1,'quote':quote}],'against':[],'reason':'SYNTH first public reason.'}]}
    return snapshot,classification,first


def plan_for(dependency=False, response_contract=None, *, supports=1):
    snapshot,classification,first=fixture(dependency)
    if supports != 1: first['claims'][0]['support']*=supports
    stub=ModuleType(counter.__package__+'.proof_classification')
    stub.validate_classification=lambda *args,**kwargs:None
    kwargs={'revision_id':1,'job_id':2,'job_key':'SYNTH-job','owner_id':3,'first_receipt_id':4,
        'model':'SYNTH','effort':'none'}
    if response_contract is not None: kwargs['response_contract']=response_contract
    with patch.dict('sys.modules',{stub.__name__:stub}):
        return counter.build_adversarial_plan(snapshot,classification,first,**kwargs)


def raw_for(plan):
    batch=plan['batches'][0]
    v3=plan.get('response_contract')=='dv.adversarial-response/3'
    claims={}
    for claim in plan['first_result']['claims']:
        if claim['id'] not in batch['claim_ids']:continue
        relations={str(i):{'support_sha256':sha256_digest(support),'relation':'supports','reason':'SYNTH support review.'}
            for i,support in enumerate(claim['support'])}
        claims[claim['id']]={'conclusion':'no_objection','reason':'SYNTH public adversarial reason.','support_relations':relations}
    axes={axis:{'status':'not_applicable' if plan['snapshot'].get('conclusion') and axis=='affiliation_change' else 'not_found',
        'reason':'SYNTH received-only scope; never absence proof.','searched_passage_ids':[1],'findings':[]}
        for axis in counter.AXES}
    value={'contract':'dv.adversarial-response/3' if v3 else (
        counter.ADVERSARIAL_RESPONSE_V2 if plan['snapshot'].get('conclusion') else counter.ADVERSARIAL_RESPONSE),
        'fingerprint':plan['fingerprint'],'batch_id':batch['batch_id'],'claims':claims,'axes':axes,
        'limitations':['SYNTH contract test; no genuine independent judgment.']}
    if not v3:
        value['claims']=[{'claim_id':cid,'conclusion':c['conclusion'],'reason':c['reason'],
            'support_relations':[{'support_index':i,'relation':r['relation'],'reason':r['reason'],
                'passage_id':plan['first_result']['claims'][0]['support'][i]['passage_id'],
                'quote':plan['first_result']['claims'][0]['support'][i]['quote']}
                for i,r in enumerate(c['support_relations'].values())]} for cid,c in claims.items()]
        value['axes']=[{'axis':axis,**data} for axis,data in axes.items()]
    return value


class AdversarialResponseV3Tests(unittest.TestCase):
    def setUp(self):
        self.plan=plan_for(True,'dv.adversarial-response/3')
        self.batch=self.plan['batches'][0]
        self.raw=raw_for(self.plan)

    def rejected(self, value, code, plan=None):
        with self.assertRaisesMessage(ValidationError,code):
            counter.validate_adversarial_response(plan or self.plan,self.batch,value)

    def assertRaisesMessage(self, exception, message):
        return self.assertRaisesRegex(exception,message)

    def test_v3_raw_identity_preserved(self):
        before=deepcopy(self.raw)
        self.assertIs(counter.validate_adversarial_response(self.plan,self.batch,self.raw),self.raw)
        self.assertEqual(self.raw,before)

    def test_opt_in_changes_plan_packet_and_input_without_changing_snapshot(self):
        legacy=plan_for(True)
        self.assertEqual(legacy['fingerprint'],self.plan['fingerprint'])
        self.assertNotEqual(legacy['plan_digest'],self.plan['plan_digest'])
        self.assertNotEqual(legacy['batches'][0]['packet_digest'],self.batch['packet_digest'])
        self.assertNotIn('response_contract',legacy)
        self.assertNotIn('support_references',legacy['batches'][0]['packet'])
        self.assertEqual(self.batch['packet']['response_contract'],'dv.adversarial-response/3')

    def test_without_conclusion_opt_in_works_no_not_applicable(self):
        plan=plan_for(False,'dv.adversarial-response/3');raw=raw_for(plan)
        self.assertIs(counter.validate_adversarial_response(plan,plan['batches'][0],raw),raw)

    def test_plan_rebuild_and_reference_recompute(self):
        stub=ModuleType(counter.__package__+'.proof_classification');stub.validate_classification=lambda *a,**k:None
        with patch.dict('sys.modules',{stub.__name__:stub}):
            self.assertIs(counter.validate_adversarial_plan(self.plan,self.plan['classification']),self.plan)
            changed=deepcopy(self.plan)
            changed['batches'][0]['packet']['support_references']['SYNTH-claim-1']['0']='b'*64
            with self.assertRaisesMessage(ValidationError,'adversarial_plan_content_changed'):
                counter.validate_adversarial_plan(changed,changed['classification'])

    def test_unknown_opt_in_contract_refused(self):
        with self.assertRaisesMessage(ValidationError,'invalid_adversarial_response_contract'):
            plan_for(True,'dv.adversarial-response/999')

    def test_v2_response_cannot_enter_v3(self):
        self.rejected(raw_for(plan_for(True)),'adversarial_response_binding_or_bound_changed')

    def test_v3_response_cannot_enter_legacy_v2(self):
        legacy=plan_for(True)
        with self.assertRaisesMessage(ValidationError,'adversarial_response_binding_or_bound_changed'):
            counter.validate_adversarial_response(legacy,legacy['batches'][0],self.raw)

    def test_top_extra_fields_refused(self):
        self.raw['renamed_refusal']='SYNTH'
        self.rejected(self.raw,'adversarial_response_binding_or_bound_changed')

    def test_fingerprint_refused(self):
        self.raw['fingerprint']='b'*64;self.rejected(self.raw,'adversarial_response_binding_or_bound_changed')

    def test_batch_refused(self):
        self.raw['batch_id']='other';self.rejected(self.raw,'adversarial_response_binding_or_bound_changed')

    def test_claim_missing_refused(self):
        self.raw['claims'].clear();self.rejected(self.raw,'adversarial_claim_coverage_incomplete')

    def test_claim_extra_refused(self):
        self.raw['claims']['foreign']=deepcopy(self.raw['claims']['SYNTH-claim-1'])
        self.rejected(self.raw,'adversarial_claim_coverage_incomplete')

    def test_claim_list_refused(self):
        self.raw['claims']=[];self.rejected(self.raw,'adversarial_claim_coverage_incomplete')

    def test_claim_id_extra_refused(self):
        self.raw['claims']['SYNTH-claim-1']['claim_id']='SYNTH-claim-1'
        self.rejected(self.raw,'invalid_adversarial_claim')

    def test_relation_missing_refused(self):
        self.raw['claims']['SYNTH-claim-1']['support_relations'].clear()
        self.rejected(self.raw,'adversarial_selected_support_not_examined')

    def test_relation_index_alias_refused(self):
        rel=self.raw['claims']['SYNTH-claim-1']['support_relations'];rel['00']=rel.pop('0')
        self.rejected(self.raw,'adversarial_support_index_changed')

    def test_wrong_support_digest_refused(self):
        self.raw['claims']['SYNTH-claim-1']['support_relations']['0']['support_sha256']='b'*64
        self.rejected(self.raw,'adversarial_support_digest_changed')

    def test_quote_injection_refused(self):
        self.raw['claims']['SYNTH-claim-1']['support_relations']['0']['quote']='SYNTH shortened quote'
        self.rejected(self.raw,'invalid_adversarial_support_relation')

    def test_support_passage_injection_refused(self):
        self.raw['claims']['SYNTH-claim-1']['support_relations']['0']['passage_id']=1
        self.rejected(self.raw,'invalid_adversarial_support_relation')

    def test_axes_missing_refused(self):
        self.raw['axes'].pop(counter.AXES[0]);self.rejected(self.raw,'adversarial_eight_axes_required')

    def test_axes_extra_refused(self):
        self.raw['axes']['foreign']=deepcopy(self.raw['axes'][counter.AXES[0]])
        self.rejected(self.raw,'adversarial_eight_axes_required')

    def test_axes_list_refused(self):
        self.raw['axes']=[];self.rejected(self.raw,'adversarial_eight_axes_required')

    def test_axis_name_injection_refused(self):
        self.raw['axes'][counter.AXES[0]]['axis']=counter.AXES[0]
        self.rejected(self.raw,'invalid_adversarial_axis')

    def test_necessary_axis_not_applicable_refused(self):
        self.raw['axes'][counter.AXES[0]]['status']='not_applicable'
        self.rejected(self.raw,'necessary_adversarial_axis_cannot_be_downgraded')

    def test_optional_axis_not_found_refused(self):
        self.raw['axes']['affiliation_change']['status']='not_found'
        self.rejected(self.raw,'unrequired_adversarial_axis_requires_explicit_not_applicable')

    def test_scope_omitted_refused(self):
        self.raw['axes'][counter.AXES[0]]['searched_passage_ids']=[]
        self.rejected(self.raw,'adversarial_received_material_scope_not_covered')

    def test_found_without_findings_refused(self):
        self.raw['axes'][counter.AXES[0]]['status']='found'
        self.rejected(self.raw,'adversarial_axis_findings_inconsistent')

    def test_negative_relation_cannot_be_promoted(self):
        self.raw['claims']['SYNTH-claim-1']['support_relations']['0']['relation']='context_only'
        self.rejected(self.raw,'adversarial_unfavorable_relation_cannot_be_promoted')

    def test_context_relation_qualified_retained(self):
        claim=self.raw['claims']['SYNTH-claim-1'];claim['conclusion']='qualified';claim['support_relations']['0']['relation']='context_only'
        report=counter.aggregate_adversarial(self.plan,{self.batch['batch_id']:self.raw})
        self.assertEqual(report['responses'],[self.raw])
        self.assertEqual(report['findings'][0]['proof']['quote'],self.plan['first_result']['claims'][0]['support'][0]['quote'])
        self.assertEqual(report['summary']['status'],'objections_received')
        self.assertIs(report['summary']['publishable'],False)
        self.assertIs(report['summary']['counterevidence_complete'],False)

    def test_authentic_quote_irrelevant_remains_objection_a3(self):
        claim=self.raw['claims']['SYNTH-claim-1'];claim['conclusion']='qualified';claim['support_relations']['0']['relation']='irrelevant'
        report=counter.aggregate_adversarial(self.plan,{self.batch['batch_id']:self.raw})
        self.assertEqual(report['findings'][0]['code'],'adversarial_selected_support_irrelevant')
        self.assertEqual(report['summary']['status'],'objections_received')
        self.assertIs(report['summary']['publishable'],False)
        self.assertEqual(self.plan['first_result']['claims'][0]['verdict'],'ETAYE_DANS_PERIMETRE')

    def test_against_finding_original_quote_retained(self):
        axis=self.raw['axes'][counter.AXES[0]];axis['status']='found';axis['findings']=[{
            'claim_id':'SYNTH-claim-1','passage_id':1,'quote':'SYNTH avant.','relation':'qualifies','reason':'SYNTH adverse context.'}]
        report=counter.aggregate_adversarial(self.plan,{self.batch['batch_id']:self.raw})
        self.assertEqual(report['responses'],[self.raw])
        self.assertEqual(report['findings'][0]['proof']['quote'],'SYNTH avant.')

    def test_thirteen_quote_copies_replaced_by_exact_references(self):
        plan=plan_for(True,'dv.adversarial-response/3',supports=13);raw=raw_for(plan)
        counter.validate_adversarial_response(plan,plan['batches'][0],raw)
        self.assertEqual(len(raw['claims']['SYNTH-claim-1']['support_relations']),13)
        self.assertNotIn('quote',raw['claims']['SYNTH-claim-1']['support_relations']['0'])
        raw['claims']['SYNTH-claim-1']['support_relations']['12']['support_sha256']='c'*64
        with self.assertRaisesMessage(ValidationError,'adversarial_support_digest_changed'):
            counter.validate_adversarial_response(plan,plan['batches'][0],raw)

    def test_packet_response_contract_cannot_be_downgraded(self):
        batch=deepcopy(self.batch);batch['packet']['response_contract']=counter.ADVERSARIAL_RESPONSE_V2
        with self.assertRaisesMessage(ValidationError,'adversarial_response_contract_mismatch'):
            counter.validate_adversarial_response(self.plan,batch,self.raw)

    def test_packet_reference_tampering_refused(self):
        batch=deepcopy(self.batch);batch['packet']['support_references']['SYNTH-claim-1']['0']='b'*64
        with self.assertRaisesMessage(ValidationError,'adversarial_support_reference_plan_changed'):
            counter.validate_adversarial_response(self.plan,batch,self.raw)

    def test_explicit_none_plan_marker_cannot_downgrade(self):
        plan=plan_for(True);raw=raw_for(plan);plan['response_contract']=None
        with self.assertRaisesMessage(ValidationError,'adversarial_response_contract_mismatch'):
            counter.validate_adversarial_response(plan,plan['batches'][0],raw)

    def test_explicit_none_packet_marker_cannot_downgrade(self):
        plan=plan_for(True);raw=raw_for(plan);plan['batches'][0]['packet']['response_contract']=None
        with self.assertRaisesMessage(ValidationError,'adversarial_response_contract_mismatch'):
            counter.validate_adversarial_response(plan,plan['batches'][0],raw)

    def test_v3_searched_scope_order_exact(self):
        plan=deepcopy(self.plan);batch=deepcopy(self.batch);batch['searched_passage_ids']=[1,2]
        raw=deepcopy(self.raw)
        for axis in raw['axes'].values(): axis['searched_passage_ids']=[1,2]
        raw['axes'][counter.AXES[0]]['searched_passage_ids']=[2,1]
        with self.assertRaisesMessage(ValidationError,'adversarial_received_material_scope_not_covered'):
            counter.validate_adversarial_response(plan,batch,raw)

    def test_v3_unavailable_scope_stays_received_exact(self):
        self.raw['axes'][counter.AXES[0]]['status']='unavailable'
        self.raw['axes'][counter.AXES[0]]['searched_passage_ids']=[]
        self.rejected(self.raw,'adversarial_received_material_scope_not_covered')

    def test_no_support_is_explicit_empty_map(self):
        plan=plan_for(True,'dv.adversarial-response/3',supports=0);raw=raw_for(plan)
        self.assertEqual(raw['claims']['SYNTH-claim-1']['support_relations'],{})
        counter.validate_adversarial_response(plan,plan['batches'][0],raw)

    def test_bounded_v3_not_expanded_wire_budget(self):
        plan=plan_for(True,'dv.adversarial-response/3',supports=13);raw=raw_for(plan)
        plan['max_output_bytes']=len(counter.canonical_bytes(raw))
        counter.validate_adversarial_response(plan,plan['batches'][0],raw)
        plan['max_output_bytes']-=1
        with self.assertRaisesMessage(ValidationError,'adversarial_response_binding_or_bound_changed'):
            counter.validate_adversarial_response(plan,plan['batches'][0],raw)

    def test_legacy_golden_byte_shapes_unchanged(self):
        for dependency in (False,True):
            plan=plan_for(dependency);raw=raw_for(plan)
            report=counter.aggregate_adversarial(plan,{plan['batches'][0]['batch_id']:raw})
            key='2' if dependency else '1'
            self.assertEqual(sha256_digest(plan),LEGACY_GOLDEN[key]['plan_sha256'])
            self.assertEqual(sha256_digest(raw),LEGACY_GOLDEN[key]['response_sha256'])
            self.assertEqual(sha256_digest(report),LEGACY_GOLDEN[key]['report_sha256'])


if __name__=='__main__':
    unittest.main()
