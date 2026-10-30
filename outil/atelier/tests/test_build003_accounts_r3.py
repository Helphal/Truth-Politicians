"""Contre-revue BUILD-003 ACCOUNTS R3 — attribution éditoriale + refus d'ACTION admin."""
from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import (
    Declaration,
    DeclarationMeasureLink,
    DocumentaryQualification,
    EvidencePassage,
    Measure,
    Person,
    UserProfile,
)
from atelier.services.accounts import (
    create_account,
    public_attribution_for,
    regenerate_login_pseudonym,
)
from atelier.services.thematic_qualification import record_declaration_measure_link
from atelier.templatetags.dv_accounts import dv_attribution
from atelier.tests.support import AtelierTestCase

PRIVATE_HEADER = {"HTTP_X_DV_PRIVATE_ENTRY": "1"}


@override_settings(
    DV_NETWORK_MODE="disabled",
    DV_PRIVATE_ENTRY_MODE="header",
    DV_TRUST_PRIVATE_ENTRY_PROXY=True,
    DV_PRIVATE_ENTRY_HEADER_META="HTTP_X_DV_PRIVATE_ENTRY",
    DV_PRIVATE_ENTRY_EXPECTED_VALUE="1",
)
class Build003AccountsR3Tests(AtelierTestCase):
    """R3-01 attribution après rotation ; R3-02 POST action admin refusé."""

    def _make_editor_and_admin(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        admin_user.set_password("AdminR3Proof-99!")
        admin_user.dv_profile.must_change_password = False
        admin_user.dv_profile.save()
        admin_user.save()

        editor = create_account(
            role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True
        )
        editor_user = get_user_model().objects.get(pk=editor.user_id)
        editor_user.set_password("EditorR3Proof-99!")
        editor_user.dv_profile.must_change_password = False
        editor_user.dv_profile.save()
        editor_user.save()
        return admin_user, editor_user, editor.login_pseudonym

    def _make_synthetic_declaration_and_measure(self):
        """Objets documentaires minimaux synthétiques (aucune comparaison politique réelle)."""
        doc = self.make_document(source_id=self.unique("R3-SRC"))
        quote = "Citation synthétique R3 sans enjeu politique réel."
        passage = EvidencePassage.objects.create(
            document=doc,
            exact_text=quote,
            locator="r3-fixture",
            context="Fixture BUILD-003 R3.",
            public_url="https://example.test/r3-fixture",
        )
        person = Person.objects.create(
            stable_id=self.unique("R3-PERSON"),
            display_name="Locuteur fictif R3",
            institutional_ids={"test": self.unique("R3-ID")},
        )
        declaration = Declaration.objects.create(
            stable_id=self.unique("R3-DECL"),
            speaker_person=person,
            speaker_label=person.display_name,
            capacity="identité de test",
            exact_quote=quote,
            paraphrase="Paraphrase synthétique R3.",
            occurred_on=date(2024, 6, 1),
            source_passage=passage,
            medium=Declaration.Medium.PRESS,
            source_role=Declaration.SourceRole.ORIGINAL,
            position_scope=Declaration.PositionScope.PERSONAL,
        )
        scrutiny = self.make_scrutiny(self.make_document(source_id=self.unique("R3-OFF")))
        measure = Measure.objects.create(
            stable_id=self.unique("R3-MEASURE"),
            text_version=scrutiny.submitted_text,
            title="Mesure synthétique R3",
            locator="art-r3",
            mechanism="Mécanisme de test.",
            plain_summary="Résumé synthétique.",
            concrete_effect="Effet concret de test uniquement.",
        )
        return declaration, measure, passage

    def test_r3_01_editorial_attribution_survives_pseudonym_rotation(self):
        admin_user, editor_user, old_pseudo = self._make_editor_and_admin()
        declaration, measure, passage = self._make_synthetic_declaration_and_measure()

        link = record_declaration_measure_link(
            user=editor_user,
            declaration=declaration,
            measure=measure,
            stance=DeclarationMeasureLink.Stance.SUPPORT,
            qualification_status=DocumentaryQualification.QUALIFIED,
            evidence=passage,
            rationale="Qualification synthétique R3 — preuve d'attribution recorded_by.",
        )
        self.assertIsNotNone(link.recorded_by_id)
        recorded_by_id = link.recorded_by_id
        editor_pk = editor_user.pk
        user_count_before = get_user_model().objects.count()
        internal_uid = editor_user.dv_profile.internal_uid

        new_pseudo = regenerate_login_pseudonym(
            target_user=editor_user, actor=admin_user
        )
        self.assertNotEqual(new_pseudo, old_pseudo)

        reloaded = DeclarationMeasureLink.objects.select_related(
            "recorded_by", "recorded_by__dv_profile"
        ).get(pk=link.pk)
        self.assertEqual(reloaded.recorded_by_id, recorded_by_id)
        self.assertEqual(reloaded.recorded_by.pk, editor_pk)
        self.assertEqual(reloaded.recorded_by.username, new_pseudo)
        self.assertEqual(
            reloaded.recorded_by.dv_profile.internal_uid, internal_uid
        )
        self.assertEqual(get_user_model().objects.count(), user_count_before)
        self.assertEqual(
            get_user_model().objects.filter(pk=editor_pk).count(), 1
        )
        self.assertFalse(
            get_user_model().objects.filter(username=old_pseudo).exists()
        )

        # UI historique : auteur toujours résoluble (filtre + page qualification).
        label = dv_attribution(reloaded.recorded_by)
        self.assertTrue(label)
        self.assertNotEqual(label, "Import antérieur non attribué")
        self.assertEqual(label, public_attribution_for(reloaded.recorded_by))

        client = Client()
        editor_user.refresh_from_db()
        editor_user.set_password("EditorR3Proof-99!")
        editor_user.dv_profile.must_change_password = False
        editor_user.dv_profile.save()
        editor_user.save()
        client.force_login(editor_user)
        history = client.get(reverse("thematic_qualification"), **PRIVATE_HEADER)
        self.assertEqual(history.status_code, 200)
        self.assertContains(history, "Saisi par")
        self.assertContains(history, label)
        self.assertNotContains(history, old_pseudo)

    def test_r3_02_non_admin_post_action_forbidden_without_mutation(self):
        admin_user, editor_user, _ = self._make_editor_and_admin()
        # Cible distincte : l'éditeur tente une action destructive sur un autre compte.
        target_creds = create_account(
            role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True
        )
        target = get_user_model().objects.get(pk=target_creds.user_id)
        target.set_password("TargetR3Proof-99!")
        target.dv_profile.must_change_password = False
        target.dv_profile.save()
        target.save()

        before = {
            "account_status": target.dv_profile.account_status,
            "role": target.dv_profile.role,
            "session_epoch": target.dv_profile.session_epoch,
            "login_pseudonym": target.dv_profile.login_pseudonym,
            "username": target.username,
            "password": target.password,
            "active": target.dv_profile.active,
        }

        # Surface GET conservée (preuve R2) : liste comptes → 403 pour non-admin.
        client = Client()
        client.force_login(editor_user)
        denied_get = client.get(reverse("accounts_list"), **PRIVATE_HEADER)
        self.assertEqual(denied_get.status_code, 403)

        # POST action destructive réelle avec CSRF / conditions normales.
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(editor_user)
        # Cookie CSRF via une page éditoriale (forms {% csrf_token %}).
        seed = csrf_client.get(reverse("thematic_qualification"), **PRIVATE_HEADER)
        self.assertEqual(seed.status_code, 200)
        csrf_token = csrf_client.cookies["dv_atelier_csrf"].value
        action_url = reverse("accounts_action", args=[target.dv_profile.pk])
        denied_post = csrf_client.post(
            action_url,
            {
                "csrfmiddlewaretoken": csrf_token,
                "action": "suspend",
            },
            **PRIVATE_HEADER,
        )
        self.assertEqual(denied_post.status_code, 403)

        # Aucune mutation du compte cible.
        target.refresh_from_db()
        target.dv_profile.refresh_from_db()
        self.assertEqual(target.dv_profile.account_status, before["account_status"])
        self.assertEqual(target.dv_profile.role, before["role"])
        self.assertEqual(target.dv_profile.session_epoch, before["session_epoch"])
        self.assertEqual(target.dv_profile.login_pseudonym, before["login_pseudonym"])
        self.assertEqual(target.username, before["username"])
        self.assertEqual(target.password, before["password"])
        self.assertEqual(target.dv_profile.active, before["active"])
        self.assertEqual(
            target.dv_profile.account_status, UserProfile.AccountStatus.ACTIVE
        )

        # Variante reset (irréversible) : même refus + pas de mutation.
        seed = csrf_client.get(reverse("thematic_qualification"), **PRIVATE_HEADER)
        self.assertEqual(seed.status_code, 200)
        csrf_token = csrf_client.cookies["dv_atelier_csrf"].value
        denied_reset = csrf_client.post(
            action_url,
            {
                "csrfmiddlewaretoken": csrf_token,
                "action": "reset",
                "confirm": "1",
            },
            **PRIVATE_HEADER,
        )
        self.assertEqual(denied_reset.status_code, 403)
        target.refresh_from_db()
        target.dv_profile.refresh_from_db()
        self.assertEqual(target.dv_profile.session_epoch, before["session_epoch"])
        self.assertEqual(target.password, before["password"])
        self.assertEqual(target.dv_profile.login_pseudonym, before["login_pseudonym"])
