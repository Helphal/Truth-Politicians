"""Independent dissemination counterexamples; SYNTH only, no application writes."""
from copy import deepcopy
import json
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from atelier.tests import support
from atelier.services.diffusion import selected_public_payload, project_verified_content, validate_diffusion_selection

PRIVATE = 'SYNTH-PRIVATE-ARCHIVE-MUST-NOT-LEAVE'


def private_payload():
    source = {'source_id':'SYNTH-SOURCE','version_id':'source-r1','sha256':'a'*64,
              'public_url':'https://www.senat.fr/synth-source'}
    base= support.AtelierTestCase().payload('speech_vote')
    payload= {'public_contract_version':'dv.content/0.5.0-diffusion-draft.1',
        'title':'SYNTH titre','short_answer':'SYNTH constat','main_caveat':'SYNTH réserve',
        'reasoning':'SYNTH raisonnement public','result':'convergence','declaration_quote':'SYNTH citation',
        'declaration_source_url':source['public_url'],'attribution':{'person_id':'SYN:PERSON','organization_id':None,'speaker_label':'SYNTH auteur','capacity':'SYNTH personnelle'},
        'evidence':[{'source_version':source,'locator':'SYNTH paragraphe 2','transcription_status':'source_text',
                     'exact_quote':PRIVATE+' '+('contexte privé '*400)+' SYNTH citation fin privée.'}],
        'diffusion_selection':{'contract':'dv.diffusion-selection/1','passages':[{
            'evidence_index':0,'exact_quote':'SYNTH citation','attribution':'SYNTH auteur, Sénat'}],
            'public_note':'SYNTH réserve de diffusion'}}
    base.update(payload)
    base['qualification_material']['declaration_source']['locator']=PRIVATE
    return base


class DiffusionProjectionCounterreview(SimpleTestCase):
    def assert_omitted_or_rejected(self, payload):
        try: result=selected_public_payload(payload)
        except ValidationError: return
        self.assertNotIn(PRIVATE,json.dumps(result,ensure_ascii=False))

    def test_long_private_evidence_and_qualification_material_are_omitted(self):
        p=private_payload();result=selected_public_payload(p)
        self.assertEqual(result['evidence'][0]['exact_quote'],'SYNTH citation')
        self.assertNotIn(PRIVATE,json.dumps(result,ensure_ascii=False))
        self.assertIn(PRIVATE,p['evidence'][0]['exact_quote'])
        self.assertIn('qualification_material',p)

    def test_unknown_nested_archive_fields_cannot_cross_public_projection(self):
        seed=private_payload()
        seed['catalogue']={k:[] for k in ('scope_scrutiny_ids','persons','organizations','affiliations','mandates',
            'text_versions','legislative_dossiers','scrutinies','vote_observations','exclusions')}
        seed['catalogue']['exclusions']=['SYNTH périmètre vide, aucune exhaustivité']
        seed['catalogue']['scope_scrutiny_ids']=['SYNTH:VOTE']
        seed['catalogue']['pagination']={'limit':25,'offset':0,'returned_count':0,
            'total_count_in_materialized_scope':0,'complete_for_materialized_scope':True}
        seed['counterevidence']=[{'statement':'SYNTH réserve','source_url':seed['declaration_source_url'],'effect':'SYNTH limite'}]
        seed['acts']=[{'scrutiny_id':'SYNTH:VOTE','included':True,'reason':'SYNTH acte','chronological_relation':'same_day',
            'occurred_on':'2024-01-30','official_title':'SYNTH titre','official_text_url':seed['official_text_url'],
            'official_vote_url':seed['official_vote_url'],'source_version':deepcopy(seed['evidence'][0]['source_version'])}]
        self.assertEqual(selected_public_payload(seed)['evidence'][0]['exact_quote'],'SYNTH citation')
        for path in [('catalogue',),('coverage',),('actors','speaker'),('attribution',),('counterevidence',0),('acts',0)]:
            with self.subTest(path=path):
                p=deepcopy(seed);target=p
                for part in path:target=target[part]
                target['private_archive']={'raw':PRIVATE}
                self.assert_omitted_or_rejected(p)

    def test_source_snapshot_cannot_carry_an_extra_private_archive_field(self):
        p=private_payload();source=deepcopy(p['evidence'][0]['source_version'])
        content={k:p[k] for k in ['title','short_answer','main_caveat','reasoning','result']}
        content.update(method_version='SYNTH-method',source_snapshot=[source],public_payload=p)
        self.assertEqual(project_verified_content(content)['content']['public_payload']['evidence'][0]['exact_quote'],'SYNTH citation')
        source['private_archive']=PRIVATE
        try: result=project_verified_content(content)
        except ValidationError:return
        self.assertNotIn(PRIVATE,json.dumps(result,ensure_ascii=False))

    def test_invalid_index_nonliteral_quote_and_unbound_declaration_are_refused(self):
        for field,value in [('evidence_index',True),('evidence_index',-1),('evidence_index',999),('exact_quote','SYNTH citation inventée')]:
            with self.subTest(field=field,value=value):
                p=private_payload();p['diffusion_selection']['passages'][0][field]=value
                with self.assertRaises(ValidationError):validate_diffusion_selection(p)
        p=private_payload();p['declaration_source_url']='https://www.senat.fr/synth-other'
        with self.assertRaises(ValidationError):validate_diffusion_selection(p)

    def test_citation_source_cannot_gain_extra_private_field(self):
        p=private_payload();p['evidence'][0]['source_version']['private_archive']=PRIVATE
        with self.assertRaises(ValidationError):selected_public_payload(p)
