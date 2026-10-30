"""Contre-revue BUILD-003 ACCOUNTS R1 — confidentialité, uid, session, sanitizer."""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from atelier.models import LoginThrottle, UserProfile
from atelier.services.account_audit import record_account_audit, sanitize_audit_detail
from atelier.services.accounts import (
    create_account,
    generate_pseudonym,
    regenerate_login_pseudonym,
    reset_access,
)
from atelier.services.login_throttle import (
    account_throttle_digest,
    assert_no_network_material_persisted,
    persisted_throttle_digests,
    reset_ephemeral_login_state,
)
from atelier.tests.support import AtelierTestCase

_PSEUDONYM_RE = r"^[A-Za-z]+-[A-Za-z]+-[A-Za-z]+$"
PRIVATE_HEADER = {"HTTP_X_DV_PRIVATE_ENTRY": "1"}


@override_settings(
    DV_NETWORK_MODE="disabled",
    DV_PRIVATE_ENTRY_MODE="header",
    DV_TRUST_PRIVATE_ENTRY_PROXY=True,
    DV_PRIVATE_ENTRY_HEADER_META="HTTP_X_DV_PRIVATE_ENTRY",
    DV_PRIVATE_ENTRY_EXPECTED_VALUE="1",
)
class Build003AccountsR1Tests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        LoginThrottle.objects.all().delete()
        reset_ephemeral_login_state()

    def test_r1_01_throttle_independent_of_network_identity(self):
        LoginThrottle.objects.all().delete()
        client = Client()
        pseudo = "ghost-account-r1"
        remotes = ["198.51.100.10", "203.0.113.77"]
        xffs = ["198.51.100.10, 10.0.0.1", "203.0.113.77, 10.0.0.2"]
        uas = ["Mozilla/5.0 Agent-A", "curl/8.0 Agent-B"]

        for remote, xff, ua in zip(remotes, xffs, uas):
            client.post(
                reverse("login"),
                {"username": pseudo, "password": "incorrect-long-password"},
                REMOTE_ADDR=remote,
                HTTP_X_FORWARDED_FOR=xff,
                HTTP_USER_AGENT=ua,
            )

        digests = persisted_throttle_digests()
        self.assertGreaterEqual(len(digests), 1)
        expected_account = account_throttle_digest(pseudo)
        self.assertIn(expected_account, digests)
        assert_no_network_material_persisted(
            digests=digests,
            remotes=remotes + ["10.0.0.1", "10.0.0.2"],
            user_agents=uas,
        )
        for digest in digests:
            self.assertNotIn("198.51.100", digest)
            self.assertNotIn("203.0.113", digest)
            for ua in uas:
                self.assertNotIn(ua.split()[0].casefold(), digest.casefold())

        # Même clé compte malgré réseaux distincts → verrouillage après 5 échecs cumulés.
        LoginThrottle.objects.all().delete()
        for i in range(5):
            response = client.post(
                reverse("login"),
                {"username": pseudo, "password": "incorrect-long-password"},
                REMOTE_ADDR=remotes[i % 2],
                HTTP_X_FORWARDED_FOR=xffs[i % 2],
                HTTP_USER_AGENT=uas[i % 2],
            )
            self.assertEqual(response.status_code, 200)
        locked = client.post(
            reverse("login"),
            {"username": pseudo, "password": "incorrect-long-password"},
            REMOTE_ADDR=remotes[1],
            HTTP_X_FORWARDED_FOR=xffs[1],
            HTTP_USER_AGENT=uas[1],
        )
        self.assertEqual(locked.status_code, 429)
        attempt = LoginThrottle.objects.get(key_digest=expected_account)
        self.assertIsNotNone(attempt.locked_until)
        self.assertEqual(attempt.key_digest, expected_account)

    def test_r1_02_internal_uid_immutable_on_save_and_orm(self):
        creds = create_account(role=UserProfile.Role.EDITOR, is_test_identity=True)
        user = get_user_model().objects.get(pk=creds.user_id)
        profile = user.dv_profile
        original = profile.internal_uid
        self.assertTrue(original)

        profile.internal_uid = uuid.uuid4()
        with self.assertRaises(ValidationError):
            profile.save()
        profile.refresh_from_db()
        self.assertEqual(profile.internal_uid, original)

        with self.assertRaises(ValidationError):
            UserProfile.objects.filter(pk=profile.pk).update(internal_uid=uuid.uuid4())
        profile.refresh_from_db()
        self.assertEqual(profile.internal_uid, original)

        # Contournement SQL direct : trigger SQLite si disponible.
        if connection.vendor == "sqlite":
            new_uid = str(uuid.uuid4())
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE atelier_userprofile SET internal_uid = %s WHERE id = %s",
                        [new_uid, profile.pk],
                    )
            profile.refresh_from_db()
            self.assertEqual(str(profile.internal_uid), str(original))

        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        regenerated = regenerate_login_pseudonym(target_user=user, actor=admin_user)
        user.refresh_from_db()
        self.assertNotEqual(regenerated, creds.login_pseudonym)
        self.assertEqual(user.dv_profile.internal_uid, original)

    def test_r1_03_pseudonym_fully_semantic(self):
        # Format lexical déterministe (diversité probabiliste remplacée en R2).
        name = generate_pseudonym()
        self.assertRegex(name, _PSEUDONYM_RE)
        parts = name.split("-")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertTrue(part.isalpha())
            self.assertFalse(any(ch.isdigit() for ch in part))
        created = [
            create_account(role=UserProfile.Role.EDITOR, is_test_identity=True)
            for _ in range(8)
        ]
        pseudos = {c.login_pseudonym for c in created}
        self.assertEqual(len(pseudos), 8)
        for pseudo in pseudos:
            self.assertRegex(pseudo, _PSEUDONYM_RE)

    def test_r1_04_password_change_keeps_current_session_invalidates_others(self):
        admin_creds = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin = get_user_model().objects.get(pk=admin_creds.user_id)
        password = admin_creds.temporary_password

        session_a = Client()
        login_a = session_a.post(
            reverse("login"),
            {"username": admin_creds.login_pseudonym, "password": password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(login_a.status_code, 302)

        session_b = Client()
        login_b = session_b.post(
            reverse("login"),
            {"username": admin_creds.login_pseudonym, "password": password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(login_b.status_code, 302)
        self.assertTrue(session_b.session.session_key)
        other_session_key = session_b.session.session_key
        self.assertTrue(Session.objects.filter(session_key=other_session_key).exists())

        new_password = "RemplacementSessionR1-99!"
        change = session_a.post(
            reverse("password_change_required"),
            {"new_password1": new_password, "new_password2": new_password},
            follow=True,
            **PRIVATE_HEADER,
        )
        self.assertEqual(change.status_code, 200)
        self.assertEqual(change.request["PATH_INFO"], reverse("dashboard"))
        self.assertTrue(change.context["user"].is_authenticated)

        # Requête suivante : session A reste authentifiée.
        again = session_a.get(reverse("dashboard"), **PRIVATE_HEADER)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.wsgi_request.user.is_authenticated)

        # Session B préexistante : empreinte auth invalide → plus authentifiée.
        other = session_b.get(reverse("dashboard"), **PRIVATE_HEADER)
        self.assertEqual(other.status_code, 302)
        self.assertTrue(
            "connexion" in other.url or "login" in other.url,
            msg=f"expected login redirect, got {other.url}",
        )

        # reset_access continue de révoquer les sessions.
        editor = create_account(role=UserProfile.Role.EDITOR, actor=admin, is_test_identity=True)
        target = get_user_model().objects.get(pk=editor.user_id)
        target.set_password("EditorTemp-R1-88!")
        target.dv_profile.must_change_password = False
        target.dv_profile.save(update_fields=["must_change_password", "updated_at"])
        target.save()
        ed_client = Client()
        ed_client.force_login(target)
        self.assertTrue(ed_client.session.session_key)
        epoch_before = target.dv_profile.session_epoch
        reset_access(target_user=target, actor=admin)
        target.dv_profile.refresh_from_db()
        self.assertGreater(target.dv_profile.session_epoch, epoch_before)
        denied = ed_client.get(reverse("dashboard"))
        self.assertIn(denied.status_code, {302, 403})

    def test_r1_05_sanitize_strips_ip_under_innocent_keys_and_nested(self):
        cleaned = sanitize_audit_detail(
            {
                "code": "safe",
                "endpoint": "198.51.100.44",
                "nested": {
                    "label": "ok",
                    "peer": "2001:db8::1",
                    "items": ["keep", "203.0.113.9", {"hop": "127.0.0.1", "code": 1}],
                },
                "tags": ("alpha", "192.0.2.1", "beta"),
            }
        )
        self.assertEqual(cleaned.get("code"), "safe")
        self.assertNotIn("endpoint", cleaned)
        self.assertEqual(cleaned["nested"]["label"], "ok")
        self.assertNotIn("peer", cleaned["nested"])
        self.assertIn("keep", cleaned["nested"]["items"])
        self.assertNotIn("203.0.113.9", cleaned["nested"]["items"])
        self.assertEqual(cleaned["nested"]["items"][1].get("code"), 1)
        self.assertNotIn("hop", cleaned["nested"]["items"][1])
        self.assertEqual(cleaned["tags"], ["alpha", "beta"])

        event = record_account_audit(
            actor=None,
            action="probe_r1",
            target_user=None,
            success=True,
            reason="ok",
            detail={
                "summary": "all-good",
                "relay": "10.0.0.8",
                "REMOTE_ADDR": "10.0.0.8",
                "HTTP_USER_AGENT": "Evil",
                "tree": {"via": "172.16.0.1", "ok": True},
                "role": "editor",
                "access_mode": "invite",
            },
        )
        blob = str(event.detail).casefold()
        self.assertNotIn("10.0.0.8", blob)
        self.assertNotIn("172.16.0.1", blob)
        self.assertNotIn("evil", blob)
        self.assertNotIn("all-good", blob)
        self.assertNotIn("summary", event.detail)
        self.assertNotIn("tree", event.detail)
        self.assertEqual(event.detail.get("role"), "editor")
        self.assertEqual(event.detail.get("access_mode"), "invite")
        self.assertEqual(event.detail.get("reason"), "ok")
