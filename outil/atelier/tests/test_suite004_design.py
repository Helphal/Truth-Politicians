import os
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase
from django.urls import reverse

from atelier.models import EditorialDossier
from atelier.tests.support import AtelierTestCase


class DesignSystemTests(SimpleTestCase):
    def test_stylesheet_has_accessible_responsive_foundations(self):
        css = (settings.BASE_DIR / "atelier/static/atelier/app.css").read_text(encoding="utf-8")
        for marker in (
            ":root",
            ".skip-link",
            ":focus-visible",
            "@media (max-width: 700px)",
            "prefers-reduced-motion",
            ".evidence-flow",
            ".direction-switch",
            ".comparison-grid",
            ".qualification-grid",
        ):
            self.assertIn(marker, css)
        self.assertNotIn("@import url", css)

    def test_deployment_refreshes_reproducibly_timestamped_static_assets(self):
        path = Path(os.environ.get("DV_TEST_DEPLOY_COMPOSE", settings.BASE_DIR / "deploy/docker/compose.yaml"))
        if not path.is_file():
            if os.environ.get("DV_TEST_DEPLOY_COMPOSE"):
                self.fail("DV_TEST_DEPLOY_COMPOSE pointe vers un artefact absent")
            self.skipTest("Artefact de deploiement absent du paquet code ; DV_TEST_DEPLOY_COMPOSE requis")
        compose = path.read_text(encoding="utf-8")
        self.assertIn("collectstatic --noinput --clear", compose)

    def test_login_exposes_private_context_and_clear_action(self):
        response = Client().get(reverse("login"))
        self.assertContains(response, "Truth Politicians")
        self.assertContains(response, "Espace de travail sécurisé")
        self.assertContains(response, "Accéder à l’atelier")
        self.assertContains(response, 'class="skip-link"')


class DesignRenderTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_dashboard_prioritizes_attention_and_publication_boundary(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Ce qui mérite votre attention")
        self.assertContains(response, "La publication reste une frontière explicite")
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, 'class="metric-grid"')

    def test_review_prioritizes_claim_sources_limits_and_human_action(self):
        dossier, _, _ = self.make_revision(EditorialDossier.Kind.OFFICIAL_VOTE)
        dossier.data_scope = "real"
        dossier.save(update_fields=["data_scope"])
        response = self.client.get(reverse("review", args=[dossier.pk]))
        self.assertContains(response, "Ce que cette version affirme")
        self.assertContains(response, "Lire le texte officiel")
        self.assertContains(response, "Vérifier le vote officiel")
        self.assertContains(response, "Aucune revue humaine enregistrée")
        self.assertContains(response, 'class="decision-panel"')
        self.assertContains(response, 'aria-current="page"')

    def test_comparator_navigation_uses_existing_design_language(self):
        response = self.client.get(reverse("thematic_comparisons"))
        self.assertContains(response, 'class="evidence-flow"')
        self.assertContains(response, 'class="direction-switch"')
        self.assertContains(response, "Aucune conclusion automatique publiée")
