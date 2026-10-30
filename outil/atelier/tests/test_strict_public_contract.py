from copy import deepcopy
from unittest.mock import patch
from django.core.exceptions import ValidationError
from atelier.models import EditorialDossier
from atelier.services.contracts import validate_speech_vote_payload
from atelier.services.publication import evaluate_revision, validate_public_payload
from atelier.services.canonical import sha256_digest
from atelier.services.verification import structural_metadata
from .support import AtelierTestCase


class StrictPublicContractTests(AtelierTestCase):
    def test_extended_public_has_distinct_full_review_contract(self):
        from atelier.services.full_review import build_review_bundle, DVVERITE_FORMAT
        payload=self.payload('speech_vote')
        payload['public_contract_version']='dv.content/0.4.0-dvverite-draft.1'
        _,revision,_=self.make_revision('speech_vote',payload=payload)
        bundle=build_review_bundle(revision)
        self.assertEqual(bundle['format'],DVVERITE_FORMAT)
        self.assertEqual(bundle['public'],payload)

    def test_complete_fixture_and_missing_fields_are_distinguished(self):
        payload=self.payload('speech_vote')
        validate_speech_vote_payload(payload)
        for section, field in [('coverage','normalized_count'),('method_explanation','object')]:
            bad=deepcopy(payload);del bad[section][field]
            validate_public_payload('speech_vote',bad)
            with self.assertRaises(ValidationError): validate_speech_vote_payload(bad)

    def test_typed_counterevidence_theme_and_date_required(self):
        payload=self.payload('speech_vote')
        for key, value in [('counterevidence',[{'statement':'SYNTH incomplete'}]),
                           ('themes',['THEME:SYNTH']),
                           ('coverage',{**payload['coverage'],'period_start':'2024-99-99'})]:
            bad={**payload,key:value}
            with self.assertRaises(ValidationError): validate_speech_vote_payload(bad)

    def test_positive_semantic_receipt_does_not_bypass_contract(self):
        dossier,revision,_=self.make_revision('speech_vote')
        dossier.data_scope='real';dossier.save(update_fields=['data_scope'])
        revision.verification_required=True
        del revision.public_payload['coverage']['normalized_count']
        revision.snapshot_digest=sha256_digest(revision.public_payload)
        revision.save(update_fields=['verification_required','public_payload','snapshot_digest'])
        with patch('atelier.services.verification.gate',return_value=(True,'supported')):
            self.assertEqual(evaluate_revision(revision,'test').reason,'invalid_public_payload')


class PreparationMetadataTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self.payload={'acts':[{'scrutiny_id':'SYNTH:1'}],
            'catalogue':{'scope_scrutiny_ids':['SYNTH:1'],'legislative_dossiers':[{'id':'SYNTH:D'}]},
            'themes':[{'id':'THEME:SYNTH','label':'SYNTH topic','origin':'proposed','target_type':'dossier','target_id':'SYNTH:D'}],
            'coverage':{'expected_state':'unknown','expected_count':None,'retrieved_count':1,
                'normalized_count':1,'publishable_count':0,
                'statement':'Compteurs des actes sélectionnés à la préparation de cette révision ; aucune autorisation éditoriale. SYNTH'}}
        self.snapshot={'content':{'public_payload':self.payload,'source_snapshot':[]},'sources':[]}

    def allowed(self,path,value):
        return structural_metadata({'location':'/public_payload/'+path,'value':value},self.snapshot)

    def test_theme_only_proposed_pointer_label_never_exempt(self):
        self.assertTrue(self.allowed('themes/0/id','THEME:SYNTH'))
        self.assertFalse(self.allowed('themes/0/label','SYNTH topic'))
        self.payload['themes'][0]['origin']='official'
        self.assertFalse(self.allowed('themes/0/id','THEME:SYNTH'))

    def test_theme_wrong_target_and_claim_value_rejected(self):
        self.assertFalse(self.allowed('themes/0/id','THEME:OTHER'))
        self.payload['themes'][0]['target_id']='UNKNOWN'
        self.assertFalse(self.allowed('themes/0/id','THEME:SYNTH'))

    def test_counts_recomputed_and_not_political_counts(self):
        self.assertTrue(self.allowed('coverage/normalized_count',1))
        self.payload['coverage']['normalized_count']=2
        self.assertFalse(self.allowed('coverage/normalized_count',2))
        self.assertFalse(self.allowed('collective_distribution/denominator',1))

    def test_cannot_claim_permission_or_exhaustive_scope(self):
        self.payload['coverage']['publishable_count']=1
        self.assertFalse(self.allowed('coverage/publishable_count',1))
        self.payload['coverage']['publishable_count']=0
        self.payload['coverage']['expected_state']='known'
        self.assertFalse(self.allowed('coverage/expected_state','known'))
