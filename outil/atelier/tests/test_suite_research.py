from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import Client
from atelier.tests.support import AtelierTestCase
from atelier.models import InquiryCase,HumanReview,IndexedPassage
from atelier.services.research_cases import prepare_research_case

class ResearchCaseTests(AtelierTestCase):
    def test_empty_query_case_is_not_finished_analysis_or_duplicate(self):
        a,created=prepare_research_case('Mesure nouvelle fictive')
        b,again=prepare_research_case('Mesure nouvelle fictive')
        self.assertTrue(created);self.assertFalse(again);self.assertEqual(a.pk,b.pk)
        self.assertIsNone(a.candidate_evidence['conclusion']);self.assertFalse(HumanReview.objects.exists())
        self.assertEqual(a.candidate_evidence['passages']['results'],[])
    def test_timestamped_cursor_not_a_duplicate_case(self):
        result={'query':'fictive','corpus_version':'v1','next_cursor':'timestamp1','results':[]}
        with patch('atelier.services.research_cases.search_passages',return_value=result.copy()):a,_=prepare_research_case('fictive')
        result['next_cursor']='timestamp2'
        with patch('atelier.services.research_cases.search_passages',return_value=result.copy()):b,new=prepare_research_case('fictive')
        self.assertFalse(new);self.assertEqual(a.pk,b.pk)
    def test_http_creates_work_file_csrf_required(self):
        user=self.make_user();self.client.force_login(user)
        response=self.client.post('/recherche-documentaire/',{'q':'nouveau sujet fictif'})
        self.assertEqual(response.status_code,200);self.assertEqual(InquiryCase.objects.count(),1)
        client=Client(enforce_csrf_checks=True);client.force_login(user)
        self.assertEqual(client.post('/recherche-documentaire/',{'query':'fictif'}).status_code,403)
    def test_invalid_input_never_creates_case(self):
        with self.assertRaises(ValidationError):prepare_research_case('')
        self.assertFalse(InquiryCase.objects.exists())
