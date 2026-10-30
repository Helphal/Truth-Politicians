from django.test import Client,SimpleTestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from atelier.models import UserProfile
from atelier.tests.support import AtelierTestCase
from atelier.services.parsers.senate_nominal import parse_nominal

class NominalTests(SimpleTestCase):
    def test_exact_institutional_link_and_heading(self):
        raw=b'<h3>Ont vot&eacute; pour</h3><a href="/senateur/fictif_test12345a.html">Nom fictif</a><h3>Ont vot&eacute; contre</h3><a href="/senateur/autre_test12345b.html">Nom fictif</a>'
        rows=parse_nominal(raw)
        self.assertEqual([r['category'] for r in rows],['for','against'])
        self.assertNotEqual(rows[0]['institutional_ref'],rows[1]['institutional_ref'])
    def test_outside_vote_heading_not_imported(self):
        raw=b'<a href="/senateur/fictif_test12345a.html">Ignore</a><h3>Ont vot&eacute; pour</h3><a href="/senateur/autre_test12345b.html">Fictif</a>'
        self.assertEqual(len(parse_nominal(raw)),1)
    def test_duplicate_id_refused(self):
        raw=b'<h3>Ont vot&eacute; pour</h3><a href="/senateur/fictif_test12345a.html">Fictif</a>'*2
        with self.assertRaisesMessage(ValueError,'senate_duplicate_institutional_id'):parse_nominal(raw)

class NewPagesTests(AtelierTestCase):
    def test_anonymous_cannot_read_indexes(self):
        for name in ('documentary_search','staged_votes'):
            self.assertEqual(self.client.get(reverse(name)).status_code,302)
    def test_inactive_role_denied(self):
        user=get_user_model().objects.create_user(username='SYNTHETIC',password=None)
        UserProfile.objects.create(user=user,role='collector',active=False,is_test_identity=True);self.client.force_login(user)
        for name in ('documentary_search','staged_votes'):self.assertEqual(self.client.get(reverse(name)).status_code,403)
    def test_query_is_escaped_not_executed(self):
        user=get_user_model().objects.create_user(username='SYNTHETIC',password=None)
        UserProfile.objects.create(user=user,role='collector',active=True,is_test_identity=True);self.client.force_login(user)
        response=self.client.get(reverse('documentary_search'),{'q':'<script>alert(1)</script>'})
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'<script>alert(1)</script>')
    def test_csrf_correction_refused_without_token(self):
        user=get_user_model().objects.create_user(username='SYNTHETIC',password=None)
        UserProfile.objects.create(user=user,role='reviewer',active=True,is_test_identity=True)
        client=Client(enforce_csrf_checks=True);client.force_login(user)
        self.assertEqual(client.post('/dossiers/1/action/',{'mode':'correct'}).status_code,403)
