import unittest
from copy import deepcopy
from link_scrutins_rec01 import adopted_candidate,subject_kind

class LinkQualificationTests(unittest.TestCase):
    def setUp(self):
        self.rid='DV:SCRUTIN:AN:VTANR5L17V42'
        self.version='DV:VERSION:AN:PRJLANR5L17B0042'
        self.chain={'act_code':'AN1-DEBATS-DEC','vote_ref':'VTANR5L17V42',
                    'conclusion':{'libelle':'Adopté'},'texts':[{'typeTexte':'BTA','refTexteAssocie':'PRJLANR5L17B0042'}]}
    def test_positive_adopted_result(self):
        self.assertEqual(adopted_candidate('whole_text',self.rid,self.chain,{self.version}),self.version)
    def test_non_adopted_or_missing_conclusion(self):
        for label in ('Non adopté','Rejeté','Pas adopté',''):
            chain=deepcopy(self.chain);chain['conclusion']={'libelle':label}
            self.assertIsNone(adopted_candidate('whole_text',self.rid,chain,{self.version}))
    def test_other_ballot_and_chamber(self):
        for rid in ('DV:SCRUTIN:AN:VTANR5L17V43','SENAT:SCRUTIN:2025:42'):
            self.assertIsNone(adopted_candidate('whole_text',rid,self.chain,{self.version}))
        chain=deepcopy(self.chain);chain['act_code']='SN1-DEBATS-DEC'
        self.assertIsNone(adopted_candidate('whole_text',self.rid,chain,{self.version}))
    def test_amendment_article_motion_never_inherit_dossier_text(self):
        for kind in ('amendment','article','motion','other_or_unknown'):
            self.assertIsNone(adopted_candidate(kind,self.rid,self.chain,{self.version}))
    def test_ambiguous_missing_or_unreceived_text(self):
        for texts in ([],self.chain['texts']*2,[{'typeTexte':'BTA','refTexteAssocie':None}]):
            chain=deepcopy(self.chain);chain['texts']=texts
            self.assertIsNone(adopted_candidate('whole_text',self.rid,chain,{self.version}))
        self.assertIsNone(adopted_candidate('whole_text',self.rid,self.chain,set()))
    def test_title_classifies_only_subject(self):
        self.assertEqual(subject_kind("L'amendement portant sur l'ensemble du projet"),'amendment')
        self.assertEqual(subject_kind("L'ensemble du projet de loi"),'whole_text')
        self.assertEqual(subject_kind("Sur l'article 2"),'article')

if __name__=='__main__':unittest.main()
