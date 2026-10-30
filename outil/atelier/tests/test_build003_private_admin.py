"""PRIVATE-ADMIN-1 — entrée privée admin, secrets forts, refus public."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import AuditEvent, LoginThrottle, UserProfile
from atelier.services.accounts import (
    create_account,
    generate_temporary_password,
    reset_access,
    revoke_sessions,
)
from atelier.services.login_throttle import reset_ephemeral_login_state
from atelier.services.private_admin_entry import is_private_admin_entry
from atelier.tests.support import AtelierTestCase

PRIVATE_HEADER = {"HTTP_X_DV_PRIVATE_ENTRY": "1"}


@override_settings(
    DV_NETWORK_MODE="disabled",
    DV_PRIVATE_ENTRY_MODE="header",
    DV_TRUST_PRIVATE_ENTRY_PROXY=True,
    DV_PRIVATE_ENTRY_HEADER_META="HTTP_X_DV_PRIVATE_ENTRY",
    DV_PRIVATE_ENTRY_EXPECTED_VALUE="1",
)
class PrivateAdminOneTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        LoginThrottle.objects.all().delete()
        reset_ephemeral_login_state()
    def _admin(self):
        creds = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        user = get_user_model().objects.get(pk=creds.user_id)
        user.set_password("AdminMotDePasseFort-999!")
        user.dv_profile.must_change_password = False
        user.dv_profile.save()
        user.save()
        return user, "AdminMotDePasseFort-999!", creds

    def _editor(self, actor):
        creds = create_account(role=UserProfile.Role.EDITOR, actor=actor, is_test_identity=True)
        user = get_user_model().objects.get(pk=creds.user_id)
        user.set_password("EditorMotDePasseFort-999!")
        user.dv_profile.must_change_password = False
        user.dv_profile.save()
        user.save()
        return user, "EditorMotDePasseFort-999!"

    def test_quick_admin_create_strong_secret_and_weak_rejected(self):
        admin, _, _ = self._admin()
        client = Client()
        client.force_login(admin)
        response = client.post(
            reverse("accounts_create"),
            {
                "role": "editor",
                "access_mode": "temp_password",
                "confirm_create": "on",
            },
            **PRIVATE_HEADER,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "affichage unique")
        self.assertNotIn("dv_last_reset_credentials", client.session)
        secret = generate_temporary_password()
        validate_password(secret)
        with self.assertRaises(ValidationError):
            validate_password("short")
        with self.assertRaises(ValidationError):
            validate_password("alllowercasetoolong1")
        with self.assertRaises(ValidationError):
            validate_password("ALLUPPERCASETOOLONG1")
        with self.assertRaises(ValidationError):
            validate_password("NoSpecialCharHere12")

    def test_mandatory_first_login_password_change_and_reset_sessions(self):
        admin_creds = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin = get_user_model().objects.get(pk=admin_creds.user_id)
        self.assertTrue(admin.dv_profile.must_change_password)
        client = Client()
        login = client.post(
            reverse("login"),
            {"username": admin_creds.login_pseudonym, "password": admin_creds.temporary_password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.url, reverse("password_change_required"))
        change = client.post(
            reverse("password_change_required"),
            {
                "new_password1": "RemplacementObligatoire-42!",
                "new_password2": "RemplacementObligatoire-42!",
            },
            **PRIVATE_HEADER,
        )
        self.assertEqual(change.status_code, 302)
        self.assertEqual(change.url, reverse("dashboard"))
        admin.dv_profile.refresh_from_db()
        self.assertFalse(admin.dv_profile.must_change_password)

        editor = create_account(role=UserProfile.Role.EDITOR, actor=admin, is_test_identity=True)
        target = get_user_model().objects.get(pk=editor.user_id)
        epoch_before = target.dv_profile.session_epoch
        reset = reset_access(target_user=target, actor=admin)
        target.refresh_from_db()
        self.assertTrue(target.dv_profile.must_change_password)
        self.assertGreater(target.dv_profile.session_epoch, epoch_before)
        self.assertTrue(target.check_password(reset.temporary_password))
        revoke_sessions(target_user=target, actor=admin, reason="test")

    def test_admin_refused_on_public_even_with_good_password_or_prior_session(self):
        admin, password, creds = self._admin()
        public = Client()
        refused = public.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            HTTP_X_FORWARDED_FOR="203.0.113.50",
            REMOTE_ADDR="203.0.113.50",
        )
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(public.session.get("_auth_user_id"))

        private = Client()
        ok = private.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(ok.status_code, 302)
        self.assertTrue(private.session.get("dv_admin_bound_to_private"))
        denied = private.get(reverse("dashboard"))
        self.assertEqual(denied.status_code, 403)

    def test_admin_allowed_on_simulated_private_entry(self):
        admin, password, creds = self._admin()
        client = Client()
        ok = client.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(ok.status_code, 302)
        listed = client.get(reverse("accounts_list"), **PRIVATE_HEADER)
        self.assertEqual(listed.status_code, 200)

    def test_ordinary_user_public_https_ok_but_admin_functions_forbidden(self):
        admin, _, _ = self._admin()
        editor, editor_password = self._editor(admin)
        public = Client()
        login = public.post(
            reverse("login"),
            {"username": editor.username, "password": editor_password},
        )
        self.assertEqual(login.status_code, 302)
        dash = public.get(reverse("dashboard"))
        self.assertEqual(dash.status_code, 200)
        forbidden = public.get(reverse("accounts_list"))
        self.assertEqual(forbidden.status_code, 403)
        create_forbidden = public.post(
            reverse("accounts_create"),
            {"role": "editor", "access_mode": "temp_password", "confirm_create": "on"},
        )
        self.assertEqual(create_forbidden.status_code, 403)

    def test_forged_host_and_forwarded_headers_do_not_grant_admin(self):
        admin, password, creds = self._admin()
        client = Client()
        forged = client.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            HTTP_X_FORWARDED_FOR="100.64.0.1",
            HTTP_X_FORWARDED_HOST="tailscale.local",
            REMOTE_ADDR="100.64.0.1",
            HTTP_X_DV_PRIVATE_ENTRY="0",
        )
        self.assertEqual(forged.status_code, 403)
        forged2 = client.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            REMOTE_ADDR="10.0.0.2",
            HTTP_X_FORWARDED_FOR="10.0.0.2",
        )
        self.assertEqual(forged2.status_code, 403)

    def test_no_network_identifiers_persisted_in_audit(self):
        admin, password, creds = self._admin()
        client = Client()
        client.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": "Wrong-Password-999!"},
            REMOTE_ADDR="198.51.100.20",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
            HTTP_USER_AGENT="Mozilla/5.0 PrivateAdminProbe",
            **PRIVATE_HEADER,
        )
        client.post(
            reverse("login"),
            {"username": creds.login_pseudonym, "password": password},
            REMOTE_ADDR="198.51.100.20",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
            HTTP_USER_AGENT="Mozilla/5.0 PrivateAdminProbe",
        )
        for event in AuditEvent.objects.filter(event_type__startswith="account."):
            blob = str(event.detail).casefold()
            self.assertNotIn("198.51.100.20", blob)
            self.assertNotIn("203.0.113.99", blob)
            self.assertNotIn("mozilla", blob)
            self.assertNotIn("user-agent", blob)
            self.assertNotIn("fingerprint", blob)

    def test_is_private_entry_ignores_host_xff(self):
        from django.test import RequestFactory

        factory = RequestFactory()
        req = factory.get(
            "/",
            HTTP_HOST="100.64.0.1",
            HTTP_X_FORWARDED_FOR="100.64.0.1",
            REMOTE_ADDR="100.64.0.1",
        )
        self.assertFalse(is_private_admin_entry(req))
        req2 = factory.get("/", **PRIVATE_HEADER)
        self.assertTrue(is_private_admin_entry(req2))

    def test_secrets_not_left_in_session_after_create(self):
        admin, _, _ = self._admin()
        client = Client()
        client.force_login(admin)
        client.post(
            reverse("accounts_create"),
            {
                "role": "editor",
                "access_mode": "temp_password",
                "confirm_create": "on",
            },
            **PRIVATE_HEADER,
        )
        session_blob = str(dict(client.session.items())).casefold()
        self.assertNotIn("temporary_password", session_blob)
        self.assertNotIn("invite_token", session_blob)
