"""Capture HTML privée, sans compte ou revue persistants, sur copie restaurée seulement."""
from pathlib import Path
import json
import re
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client
from django.urls import reverse
from atelier.models import EditorialDossier, HumanReview, UserProfile


def _static_html(response) -> str:
    html = response.content.decode("utf-8")
    # Pas de token CSRF/session dans la preuve statique ; aucun formulaire actif.
    html = re.sub(r'<input[^>]*name="csrfmiddlewaretoken"[^>]*>', '', html)
    html = re.sub(r'<form\b[^>]*>', '<div data-disabled-form="true">', html).replace("</form>", "</div>")
    html = html.replace('type="submit"', 'type="button" disabled')
    return re.sub(
        r'<link rel="stylesheet" href="/static/atelier/app\.css(?:\?[^\"]*)?">',
        lambda _match: '<style>' + (settings.BASE_DIR / "atelier/static/atelier/app.css").read_text(encoding="utf-8") + '</style>',
        html,
    )


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--batch", default="visual-review")

    def handle(self, *args, **options):
        root = Path(settings.RUNTIME_DIR)
        if settings.DV_ENV != "test" or settings.DV_NETWORK_MODE != "disabled" or not (root / ".dv-restored-unreconciled").exists():
            raise CommandError("isolated_restored_test_only")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", options["batch"]):
            raise CommandError("invalid_batch")
        output = root / options["batch"]
        output.mkdir(exist_ok=False)
        results = []
        before = HumanReview.objects.count()

        anonymous = Client()
        response = anonymous.get(reverse("login"))
        if response.status_code != 200:
            raise CommandError("render_failed")
        html = _static_html(response)
        (output / "login.html").write_text(html, encoding="utf-8")
        results.append(dict(kind="login", html_sha256=__import__("hashlib").sha256(html.encode()).hexdigest()))

        with transaction.atomic():
            user = get_user_model().objects.create_user(username="VISUAL-FIXTURE-ROLLBACK", password=None)
            UserProfile.objects.create(user=user, role="reviewer", active=True, is_test_identity=True)
            client = Client()
            client.force_login(user)

            for kind, route in (("dashboard", "dashboard"), ("dossiers", "dossiers")):
                response = client.get(reverse(route))
                if response.status_code != 200:
                    raise CommandError("render_failed")
                html = _static_html(response)
                (output / (kind + ".html")).write_text(html, encoding="utf-8")
                results.append(dict(kind=kind, html_sha256=__import__("hashlib").sha256(html.encode()).hexdigest()))

            for kind in ("official_vote", "vote_comparison", "speech_vote"):
                dossier = EditorialDossier.objects.filter(content_kind=kind).order_by("stable_id").first()
                if dossier is None:
                    raise CommandError("missing_review_fixture")
                revision = dossier.revisions.get(revision=dossier.current_revision)
                response = client.get(reverse("review", args=[dossier.pk]))
                if response.status_code != 200:
                    raise CommandError("render_failed")
                html = _static_html(response)
                (output / (kind + ".html")).write_text(html, encoding="utf-8")
                results.append(dict(kind=kind, revision=revision.revision, snapshot_digest=revision.snapshot_digest, html_sha256=__import__("hashlib").sha256(html.encode()).hexdigest()))
            transaction.set_rollback(True)
        if HumanReview.objects.count() != before:
            raise CommandError("review_mutation")
        self.stdout.write(json.dumps(dict(scope="static_local_render_no_wordpress", files=results, persistent_users_created=0, reviews_created=0), ensure_ascii=False))
