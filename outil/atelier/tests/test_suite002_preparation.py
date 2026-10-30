from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.models import HumanReview,EditorialDossier
from atelier.services.research_cases import prepare_research_case
from atelier.services.documentary_preparation import prepare_documentary_review

class PreparationTests(AtelierTestCase):
    def test_private_permalink_displays_saved_preparation_without_mutation(self):
        case,_=prepare_research_case('recherche enregistrée')
        prep,_=prepare_documentary_review(case,counter_queries=['contre éléments'])
        url=f'/recherches/{case.pk}/'
        self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(self.make_user())
        response=self.client.get(url)
        self.assertContains(response,prep.preparation_digest)
        self.assertContains(self.client.get('/recherche-documentaire/'),url)
        self.assertFalse(HumanReview.objects.exists())
    def test_empty_search_is_not_a_political_label(self):
        case,_=prepare_research_case('aucune pièce indexée')
        a,new=prepare_documentary_review(case,counter_queries=['conditions différentes'])
        b,repeated=prepare_documentary_review(case,counter_queries=['conditions différentes'])
        self.assertTrue(new);self.assertFalse(repeated);self.assertEqual(a.pk,b.pk)
        self.assertIsNone(a.payload['political_label']);self.assertFalse(a.payload['comparability_established'])
        self.assertFalse(HumanReview.objects.exists());self.assertFalse(EditorialDossier.objects.exists())
        with self.assertRaises(ValidationError):a.save()
    def test_unbounded_counter_queries_refused(self):
        case,_=prepare_research_case('corpus local')
        with self.assertRaises(ValidationError):prepare_documentary_review(case,counter_queries=['mot']*6)
