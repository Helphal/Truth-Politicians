"""SYNTH current guide presentation; no database or provider."""
from copy import deepcopy
from django.test import SimpleTestCase
from atelier.services.verification_review import guide_markdown


class CurrentGuideTests(SimpleTestCase):
    def setUp(self):
        self.row = {'job_id': 49, 'revision_pk': 58, 'revision': 10, 'title': 'SYNTH',
            'short_answer': 'Réponse SYNTH', 'main_caveat': 'Réserve SYNTH',
            'attribution': 'SYNTH auteur', 'date': '2026-10-03', 'normalized_locator': 'SYNTH ligne1',
            'source_version': {'source_id': 'SYNTH', 'version_id': 'SYNTH-v1', 'sha256': 'a'*64,
                'public_url': 'https://www.senat.fr/synth'}}

    def guide(self, row):
        return guide_markdown({'revisions': [row]}, preview_links={49: 'C:/SYNTH/job-49.html'})

    def test_default_historical_presentation_is_preserved(self):
        text = self.guide(self.row)
        self.assertIn('Aperçu privé historique', text)
        self.assertIn('voir le refus historique et le plan courant', text)
        self.assertIn('Résultat documentaire : non qualifié.', text)

    def test_current_received_axes_not_overridden_or_promoted(self):
        row = deepcopy(self.row)
        row['review_axes'] = {'execution': 'Couverture canonique complète',
            'documentary': 'Résultat documentaire non favorable',
            'diffusion': 'Droits non qualifiés', 'gate_reason': 'verification_not_supported'}
        before = deepcopy(row)
        text = self.guide(row)
        self.assertEqual(row, before)
        self.assertNotIn('historique', text)
        self.assertIn('Aperçu privé actuel', text)
        for value in row['review_axes'].values():
            self.assertIn(value, text)
        self.assertIn('revue personnelle', text)
        self.assertIn('autorisation de diffusion restent des contrôles distincts', text)
        row['review_axes']['documentary'] = 'Étaye dans le périmètre vérifié'
        self.assertIn('Étaye dans le périmètre vérifié', self.guide(row))
        row['review_axes']['execution'] = '<script>SYNTH</script>'
        self.assertNotIn('<script>', self.guide(row))

    def test_incomplete_axes_refused_instead_of_historical_fallback(self):
        for axes in (None, {}, {'execution': 'SYNTH'}, {'execution': 'SYNTH',
                'documentary': 'SYNTH', 'diffusion': 'SYNTH', 'gate_reason': False}):
            with self.subTest(axes=axes), self.assertRaisesRegex(ValueError, 'current_review_axes_incomplete'):
                self.guide({**self.row, 'review_axes': axes})
