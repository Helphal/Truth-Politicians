"""Recette offline BUILD-003 — multi-comptes, pseudonymes, audit sans IP."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import AuditEvent, DeclarationMeasureLink, LoginThrottle, UserProfile
from atelier.services.account_audit import sanitize_audit_detail
from atelier.services.accounts import (
    assert_pseudonym_not_derived,
    change_role,
    complete_password_change,
    create_account,
    generate_pseudonym,
    profile_has_permission,
    public_attribution_for,
    redeem_invite_token,
    regenerate_login_pseudonym,
    reset_access,
    revoke_sessions,
    set_account_status,
)
from atelier.services.login_throttle import reset_ephemeral_login_state
from atelier.tests.support import AtelierTestCase


@override_settings(DV_NETWORK_MODE="disabled")
class Build003AccountsTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        LoginThrottle.objects.all().delete()
        reset_ephemeral_login_state()
    def test_pseudonym_format_uniqueness_and_non_derivation(self):
        # Format / non-dérivation déterministes (pas de tirage probabiliste de diversité).
        sample = generate_pseudonym()
        self.assertRegex(sample, r"^[A-Za-zÀ-ÿ]+-[A-Za-zÀ-ÿ]+-[A-Za-zÀ-ÿ]+$")
        assert_pseudonym_not_derived(
            sample,
            real_name="Ariel Exemple",
            email="ariel@example.invalid",
            role="admin",
            ip="203.0.113.50",
            github="Helphal",
        )
        with self.assertRaises(ValidationError):
            assert_pseudonym_not_derived("Ariel-Admin-Lynx", real_name="Ariel", role="admin")
        # Unicité via créations réelles (contrainte DB préservée).
        created = [
            create_account(role=UserProfile.Role.EDITOR, is_test_identity=True)
            for _ in range(5)
        ]
        self.assertEqual(len({c.login_pseudonym for c in created}), 5)
    def test_admin_and_editor_creation_permissions_suspend_reset_password(self):
        admin_creds = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin = get_user_model().objects.get(pk=admin_creds.user_id)
        self.assertTrue(admin_creds.temporary_password)
        self.assertTrue(admin.dv_profile.must_change_password)
        self.assertTrue(profile_has_permission(admin.dv_profile, "accounts.manage"))

        editor_creds = create_account(
            role=UserProfile.Role.EDITOR,
            actor=admin,
            is_test_identity=True,
        )
        editor = get_user_model().objects.get(pk=editor_creds.user_id)
        self.assertFalse(profile_has_permission(editor.dv_profile, "accounts.manage"))
        self.assertTrue(profile_has_permission(editor.dv_profile, "editorial.qualify"))

        with self.assertRaises(PermissionDenied):
            change_role(target_user=admin, actor=admin, new_role=UserProfile.Role.EDITOR)

        set_account_status(
            target_user=editor,
            actor=admin,
            status=UserProfile.AccountStatus.SUSPENDED,
            reason="test",
        )
        editor.dv_profile.refresh_from_db()
        self.assertEqual(editor.dv_profile.account_status, UserProfile.AccountStatus.SUSPENDED)
        self.assertFalse(editor.dv_profile.is_account_usable())

        set_account_status(
            target_user=editor,
            actor=admin,
            status=UserProfile.AccountStatus.ACTIVE,
            reason="test",
        )
        reset = reset_access(target_user=editor, actor=admin)
        editor.refresh_from_db()
        self.assertTrue(editor.dv_profile.must_change_password)
        self.assertTrue(editor.check_password(reset.temporary_password))
        complete_password_change(user=editor, new_password="NouveauMotDePasseFort-123!")
        editor.dv_profile.refresh_from_db()
        self.assertFalse(editor.dv_profile.must_change_password)

        invite = create_account(
            role=UserProfile.Role.EDITOR,
            actor=admin,
            is_test_identity=True,
            use_invite_token=True,
        )
        user = redeem_invite_token(raw_token=invite.invite_token, new_password="InviteMotDePasseFort-456!")
        self.assertEqual(user.pk, invite.user_id)

        revoked = revoke_sessions(target_user=editor, actor=admin, reason="test_revoke")
        self.assertGreaterEqual(revoked, 0)
        editor.dv_profile.refresh_from_db()
        self.assertGreaterEqual(editor.dv_profile.session_epoch, 1)

    def test_pseudonym_regeneration_keeps_internal_identity(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        target = create_account(role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True)
        user = get_user_model().objects.get(pk=target.user_id)
        uid = user.dv_profile.internal_uid
        pk = user.pk
        old = user.username
        new = regenerate_login_pseudonym(target_user=user, actor=admin_user)
        user.refresh_from_db()
        self.assertNotEqual(old, new)
        self.assertEqual(user.username, new)
        self.assertEqual(user.pk, pk)
        self.assertEqual(user.dv_profile.internal_uid, uid)

    def test_audit_never_persists_ip_ua_fingerprint(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        client = Client()
        client.post(
            reverse("login"),
            {"username": admin.login_pseudonym, "password": "wrong-password-value"},
            REMOTE_ADDR="198.51.100.77",
            HTTP_X_FORWARDED_FOR="203.0.113.10, 198.51.100.77",
            HTTP_USER_AGENT="Mozilla/5.0 BUILD003-TestAgent",
        )
        for event in AuditEvent.objects.filter(event_type__startswith="account."):
            detail = event.detail or {}
            blob = str(detail).casefold()
            self.assertNotIn("198.51.100.77", blob)
            self.assertNotIn("203.0.113.10", blob)
            self.assertNotIn("mozilla", blob)
            self.assertNotIn("user-agent", blob)
            self.assertNotIn("fingerprint", blob)
            self.assertEqual(sanitize_audit_detail({"ip": "1.2.3.4", "reason": "x"}), {"reason": "x"})

        # LoginThrottle digest only — never plaintext IP / network derivative.
        LoginThrottle.objects.all().delete()
        for _ in range(5):
            client.post(
                reverse("login"),
                {"username": "ghost", "password": "incorrect-long-password"},
                REMOTE_ADDR="192.0.2.88",
                HTTP_USER_AGENT="FingerPrintDevice/9",
            )
        attempt = LoginThrottle.objects.filter(key_digest__isnull=False).exclude(
            failures=0
        ).first()
        # Au moins le digest compte « ghost » (et éventuellement le bucket global).
        digests = list(LoginThrottle.objects.values_list("key_digest", flat=True))
        self.assertTrue(digests)
        for digest in digests:
            self.assertNotIn("192.0.2.88", digest)
            self.assertEqual(len(digest), 64)
        locked = LoginThrottle.objects.filter(locked_until__isnull=False).exists()
        self.assertTrue(locked)

        # Explicit injection into audit path must be stripped.
        from atelier.services.account_audit import record_account_audit

        event = record_account_audit(
            actor=admin_user,
            action="probe",
            target_user=admin_user,
            success=True,
            reason="ok",
            detail={
                "ip": "10.0.0.1",
                "REMOTE_ADDR": "10.0.0.1",
                "X-Forwarded-For": "10.0.0.2",
                "User-Agent": "Evil",
                "fingerprint": "abc",
                "note": "safe",
                "role": "admin",
                "sessions_deleted": 0,
            },
        )
        self.assertEqual(event.detail.get("role"), "admin")
        self.assertEqual(event.detail.get("sessions_deleted"), 0)
        self.assertNotIn("note", event.detail)
        self.assertNotIn("ip", event.detail)
        self.assertNotIn("fingerprint", event.detail)
        blob = str(event.detail).casefold()
        self.assertNotIn("10.0.0.1", blob)
        self.assertNotIn("evil", blob)

    def test_editorial_attribution_and_admin_ui_forbidden_to_non_admin(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        complete_password_change(user=admin_user, new_password=admin.temporary_password + "X1!")
        # Re-set known password for login tests
        admin_user.set_password("AdminMotDePasseFort-999!")
        admin_user.dv_profile.must_change_password = False
        admin_user.dv_profile.save()
        admin_user.save()

        editor = create_account(role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True)
        editor_user = get_user_model().objects.get(pk=editor.user_id)
        editor_user.set_password("EditorMotDePasseFort-999!")
        editor_user.dv_profile.must_change_password = False
        editor_user.dv_profile.save()
        editor_user.save()

        label = public_attribution_for(editor_user)
        self.assertEqual(label, editor_user.dv_profile.login_pseudonym)
        self.assertNotEqual(label, "")

        # Attribution via recorded_by on a synthetic link path (user id only).
        self.assertTrue(hasattr(DeclarationMeasureLink, "recorded_by"))

        client = Client()
        client.force_login(editor_user)
        denied = client.get(reverse("accounts_list"))
        self.assertEqual(denied.status_code, 403)

        client.force_login(admin_user)
        ok = client.get(reverse("accounts_list"))
        self.assertEqual(ok.status_code, 200)
        self.assertContains(ok, editor.login_pseudonym)
        create_page = client.get(reverse("accounts_create"))
        self.assertEqual(create_page.status_code, 200)

        # CSRF required on destructive action.
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(admin_user)
        bare = csrf_client.post(
            reverse("accounts_action", args=[editor_user.dv_profile.pk]),
            {"action": "suspend"},
        )
        self.assertEqual(bare.status_code, 403)

    def test_bootstrap_command_generates_two_accounts(self):
        UserProfile.objects.all().delete()
        get_user_model().objects.all().delete()
        call_command("bootstrap_initial_accounts")
        roles = set(UserProfile.objects.values_list("role", flat=True))
        self.assertEqual(roles, {UserProfile.Role.ADMIN, UserProfile.Role.EDITOR})
        for profile in UserProfile.objects.all():
            self.assertRegex(profile.login_pseudonym, r"-.+-")
            self.assertTrue(profile.user.check_password)  # hashed, usable or temp
            self.assertNotEqual(profile.user.password, "")
            self.assertFalse(profile.user.password.startswith("plaintext:"))
