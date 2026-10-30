from copy import deepcopy
from django.test import SimpleTestCase
from atelier.services.verification import structural_metadata


class DiffusionMetadataPolicyTests(SimpleTestCase):
    def setUp(self):
        source = {'source_id':'SYNTH-SOURCE','version_id':'SYNTH-V1','sha256':'a'*64,
                  'public_url':'https://www.senat.fr/synth-document'}
        self.payload = {'public_contract_version':'dv.content/0.5.0-diffusion-draft.1',
            'declaration_quote':'SYNTH parole exacte.', 'declaration_source_url':source['public_url'],
            'evidence':[{'source_version':source,'locator':'SYNTH paragraphe 1','exact_quote':'Contexte privé. SYNTH parole exacte. Autre contexte.'}],
            'diffusion_selection':{'contract':'dv.diffusion-selection/1','passages':[{
                'evidence_index':0,'exact_quote':'SYNTH parole exacte.','attribution':'SYNTH orateur'}],'public_note':'SYNTH réserve.'}}

    def check(self, suffix, value, payload=None):
        claim={'location':'/public_payload/'+suffix,'value':value}
        return structural_metadata(claim, {'content':{'public_payload':payload or self.payload}})

    def test_only_validated_contract_and_pointer_are_technical(self):
        self.assertTrue(self.check('diffusion_selection/contract','dv.diffusion-selection/1'))
        self.assertTrue(self.check('diffusion_selection/passages/0/evidence_index',0))
        self.assertFalse(self.check('diffusion_selection/passages/0/evidence_index',1))

    def test_literal_quote_attribution_and_note_remain_semantic(self):
        for path,value in [('diffusion_selection/passages/0/exact_quote','SYNTH parole exacte.'),
            ('diffusion_selection/passages/0/attribution','SYNTH orateur'),('diffusion_selection/public_note','SYNTH réserve.')]:
            self.assertFalse(self.check(path,value))

    def test_invalid_pointer_or_excerpt_cannot_gain_metadata_exception(self):
        for field,value in [('evidence_index',True),('evidence_index',99),('exact_quote','Texte absent.'),('attribution','')]:
            bad=deepcopy(self.payload);bad['diffusion_selection']['passages'][0][field]=value
            self.assertFalse(self.check('diffusion_selection/contract','dv.diffusion-selection/1',bad))

    def test_source_url_does_not_bind_an_unrelated_declaration(self):
        bad=deepcopy(self.payload);bad['declaration_source_url']='https://www.senat.fr/synth-other'
        self.assertFalse(self.check('diffusion_selection/passages/0/evidence_index',0,bad))

    def test_known_profiles_only_and_no_alias(self):
        self.assertTrue(self.check('public_contract_version','dv.content/0.5.0-diffusion-draft.1'))
        bad=deepcopy(self.payload);bad['public_contract_version']='dv.content/latest'
        self.assertFalse(self.check('public_contract_version','dv.content/latest',bad))
