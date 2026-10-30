"""SYNTH-only tests of read-only product presentation, never personal review."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from atelier.models import HumanReview, VerificationJob, VerificationAttempt, VerificationReceipt, VerificationDelivery
from atelier.services.verification_architecture_review import (
    ASSESSMENT_SCHEMA, GATE_LABELS, LAYER_LABELS, SCHEMA, build_architecture_review,
)
from atelier.tests import test_verification as fixtures


def assessment(fingerprint):
    return {
        'schema': ASSESSMENT_SCHEMA, 'revision_fingerprint': fingerprint,
        'documentary_status': 'not_supported',
        'summary': {'short_answer': 'SYNTH réponse limitée.', 'main_caveat': 'SYNTH réserve A3 conservée.',
                    'counter_elements': ['SYNTH fragment exact sans le nom allégué.'],
                    'uncertainty': {'membership_end': None}},
        'layers': {key: {'status': 'incomplete', 'summary': 'SYNTH preuve à compléter.'} for key in LAYER_LABELS},
        'gates': {key: {'status': 'blocked', 'reason': 'SYNTH aucun droit de publication.'} for key in GATE_LABELS},
        'preflight': {'contract': 'dv.verification-preflight/1', 'status': 'hold',
                      'checks': [{'code': 'passage_not_relevant', 'status': 'hold',
                                  'locations': ['/catalogue/affiliations/0/ends_on'], 'reason': 'SYNTH A3'}],
                      'cost': {'minimum_model_phases': 2, 'proposed_ceiling': 3,
                               'actual_phases_reserved': 0, 'actual_phases_launched': 0}},
        'counterevidence': {'status': 'incomplete', 'no_result_is_not_absence_proof': True},
        'unknowns': [{'location': '/catalogue/affiliations/0/ends_on', 'state': 'unknown',
                      'explanation': 'SYNTH date d’observation, pas une date de sortie.'}],
        'editorial_rules': [{'selection_rule_id': 'SYNTH-selection/1', 'reason': 'SYNTH périmètre.',
                            'limits': ['SYNTH sélection limitée.']}],
        'objections': [{'reason': 'SYNTH A3 fragment non pertinent.', 'claim_id': 'SYNTH-CLAIM'}],
    }


class ArchitecturePresentationTests(SimpleTestCase):
    def setUp(self):
        self.job = SimpleNamespace(fingerprint='a' * 64, input_snapshot={'sources': []})
        self.report = assessment(self.job.fingerprint)

    def test_absent_stale_wrong_schema_and_missing_gate_stay_unreceived(self):
        bad_fingerprint = deepcopy(self.report); bad_fingerprint['revision_fingerprint'] = 'b' * 64
        bad_schema = deepcopy(self.report); bad_schema['schema'] = 'unknown'
        missing_gate = deepcopy(self.report); del missing_gate['gates']['rights_qualified']
        missing_layer = deepcopy(self.report); del missing_layer['layers']['semantic']
        for report in (None, {}, bad_fingerprint, bad_schema, missing_gate, missing_layer):
            with self.subTest(report=report):
                view = build_architecture_review(self.job, report)
                self.assertFalse(view['available']); self.assertTrue(view['reason'])

    def test_filter_never_hides_summary_negative_gate_or_a3(self):
        before = deepcopy(self.report)
        view = build_architecture_review(self.job, self.report, query={'problem': 'provenance'})
        self.assertEqual(view['schema'], SCHEMA)
        self.assertEqual(view['documentary_status'], 'not_supported')
        self.assertEqual(len(view['gate_rows']), 7)
        self.assertTrue(all(row['category'] == 'provenance' for row in view['page']))
        self.assertTrue(any('A3' in field['text'] for field in view['objection_fields']))
        self.assertTrue(any('réserve A3' in field['text'] for field in view['summary_fields']))
        self.assertEqual(self.report, before)

    def test_filter_allowlist_pagination_and_empty_not_absent(self):
        self.report['layers']['provenance']['certificates'] = [{'raw_value': index} for index in range(65)]
        view = build_architecture_review(self.job, self.report,
                                         query={'problem': 'provenance', 'proof_page': '2'})
        self.assertEqual(view['filtered_count'], 65); self.assertEqual(len(view['page']), 30)
        self.assertEqual(view['page'].number, 2)
        view = build_architecture_review(self.job, self.report, query={'problem': '<script>'})
        self.assertEqual(view['selected_filter'], 'all')
        self.report['unknowns'] = []
        empty = build_architecture_review(self.job, self.report, query={'problem': 'unknowns'})
        self.assertEqual(empty['filtered_count'], 0)
        self.assertEqual(empty['documentary_status'], 'not_supported')
        html = render_to_string('atelier/verification_architecture.html', {'architecture': empty})
        self.assertIn('cela ne vaut pas absence démontrée', html)

    def test_bound_canonical_assertion_and_passage_links(self):
        self.job.input_snapshot['sources'] = [{'passages': [{'id': 7}]}]
        self.report['objections'][0]['passage_id'] = 7
        legacy = {'attempts': [{'groups': [{'claims': [{'id': 'SYNTH-CLAIM', 'anchor': 'assertion-12-3'}]}]}]}
        view = build_architecture_review(self.job, self.report, query={'problem': 'objection'}, legacy_review=legacy)
        self.assertEqual({link['href'] for link in view['page'][0]['links']}, {'#assertion-12-3', '#passage-7'})

    def test_untrusted_structures_escape_and_links_are_checked(self):
        self.report['summary']['unsafe'] = {'label': '<script>SYNTH</script>',
                                         'url': 'javascript:alert(1)', 'local_path': 'C:/SYNTH/hidden'}
        view = build_architecture_review(self.job, self.report)
        html = render_to_string('atelier/verification_architecture.html', {'architecture': view})
        self.assertIn('&lt;script&gt;SYNTH&lt;/script&gt;', html)
        self.assertNotIn('<script>SYNTH</script>', html)
        self.assertNotIn('javascript:alert', html); self.assertNotIn('C:/SYNTH/hidden', html)
        self.assertNotIn('[object Object]', html); self.assertIn('Inconnu / non établi', html)
        self.assertIn('Aucune revue personnelle', html)


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ArchitecturePrivateViewTests(TestCase):
    setUp = fixtures.VerificationTests.setUp

    def counts(self):
        return tuple(model.objects.count() for model in (
            HumanReview, VerificationAttempt, VerificationReceipt, VerificationDelivery))

    @patch('atelier.verification_views._assessment', return_value=None)
    def test_authentication_and_missing_assessment_are_read_only(self, reader):
        url = reverse('verification_detail', args=[self.job.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user); before = self.counts()
        response = self.client.get(url)
        self.assertContains(response, 'Évaluation des quatre couches non reçue')
        self.assertContains(response, 'Trois axes distincts')
        self.assertEqual(before, self.counts())

    @patch('atelier.verification_views._assessment')
    def test_profile_filter_history_and_index_do_not_create_receipts(self, reader):
        reader.return_value = assessment(self.job.fingerprint)
        self.client.force_login(self.user); before = self.counts()
        response = self.client.get(reverse('verification_detail', args=[self.job.pk]), {'problem': 'unknowns'})
        self.assertContains(response, 'not_supported'); self.assertContains(response, 'SYNTH réserve A3')
        self.assertContains(response, 'Historique par révision'); self.assertContains(response, 'date de sortie')
        self.assertNotContains(response, '[object Object]')
        self.assertContains(self.client.get(reverse('verification_index')), 'Architecture des quatre couches')
        self.assertEqual(before, self.counts())

    @patch('atelier.verification_views._assessment')
    def test_new_job_same_fingerprint_cannot_hide_the_historical_negative(self, reader):
        reader.return_value = assessment(self.job.fingerprint)
        attempt = VerificationAttempt.objects.create(job=self.job, actor=self.user, number=1,
            execution_id='SYNTH:architecture-negative', provider='synthetic',
            expires_at=timezone.now()+timedelta(minutes=2), finished_at=timezone.now())
        VerificationReceipt.objects.create(job=self.job, attempt=attempt, fingerprint=self.job.fingerprint,
            result_digest='c'*64, decision='not_supported')
        latest = VerificationJob.objects.create(revision=self.revision, key='d'*64, fingerprint=self.job.fingerprint,
            policy=self.job.policy, input_snapshot=deepcopy(self.job.input_snapshot))
        self.client.force_login(self.user); before = self.counts()
        response = self.client.get(reverse('verification_detail', args=[latest.pk]), {'problem': 'rights'})
        self.assertContains(response, 'concerne cette empreinte')
        self.assertContains(response, reverse('verification_detail', args=[self.job.pk]))
        self.assertContains(response, 'not_supported')
        self.assertEqual(before, self.counts())
