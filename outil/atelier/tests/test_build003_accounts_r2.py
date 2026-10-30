"""Contre-revue BUILD-003 ACCOUNTS R2 — audit fermé, invitations, throttle, pseudonymes."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from atelier.models import AccountInviteToken, AuditEvent, LoginThrottle, UserProfile
from atelier.services.account_audit import (
    build_closed_account_detail,
    record_account_audit,
    sanitize_audit_detail,
)
from atelier.services.accounts import (
    PSEUDONYM_COMPONENT_COUNTS,
    PSEUDONYM_SPACE_SIZE,
    create_account,
    generate_pseudonym,
    regenerate_login_pseudonym,
    redeem_invite_token,
    reset_access,
    set_account_status,
)
from atelier.services.login_throttle import (
    ACCOUNT_FAILURE_LIMIT,
    ACCOUNT_WINDOW_MINUTES,
    GLOBAL_FAILURE_LIMIT,
    GLOBAL_LOCK_MINUTES,
    GLOBAL_WINDOW_MINUTES,
    account_throttle_digest,
    assert_no_network_material_persisted,
    clear_login_throttle,
    global_throttle_digest,
    is_login_throttled,
    record_login_failure,
    reset_ephemeral_login_state,
)
from atelier.tests.support import AtelierTestCase

PRIVATE_HEADER = {"HTTP_X_DV_PRIVATE_ENTRY": "1"}
_PSEUDONYM_RE = r"^[A-Za-z]+-[A-Za-z]+-[A-Za-z]+$"


@override_settings(
    DV_NETWORK_MODE="disabled",
    DV_PRIVATE_ENTRY_MODE="header",
    DV_TRUST_PRIVATE_ENTRY_PROXY=True,
    DV_PRIVATE_ENTRY_HEADER_META="HTTP_X_DV_PRIVATE_ENTRY",
    DV_PRIVATE_ENTRY_EXPECTED_VALUE="1",
)
class Build003AccountsR2Tests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        LoginThrottle.objects.all().delete()
        reset_ephemeral_login_state()

    # --- R2-01 schéma fermé ---

    def test_r2_01_closed_schema_rejects_known_bypasses_and_persists_safe(self):
        # Contournements connus : IPv6+port, IP dans texte, clé IP, UA libre, META.
        dirty = {
            "endpoint": "[2001:db8::1]:443",
            "motif": "motif depuis 198.51.100.44",
            "198.51.100.44": "as-key",
            "description": "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 Chrome/120",
            "REMOTE_ADDR": "203.0.113.9",
            "HTTP_USER_AGENT": "EvilAgent/1.0",
            "HTTP_X_FORWARDED_FOR": "198.51.100.10",
            "nested": {"peer": "2001:db8::2", "ok": True},
            "role": "editor",
            "access_mode": "temp_password",
            "sessions_deleted": 2,
            "session_epoch": 3,
            "private_entry": False,
            "note": "should-drop-free-text",
            "summary": "should-drop",
        }
        closed = build_closed_account_detail(dirty)
        self.assertEqual(closed.get("role"), "editor")
        self.assertEqual(closed.get("access_mode"), "temp_password")
        self.assertEqual(closed.get("sessions_deleted"), 2)
        self.assertEqual(closed.get("session_epoch"), 3)
        self.assertIs(closed.get("private_entry"), False)
        for forbidden in (
            "endpoint",
            "motif",
            "198.51.100.44",
            "description",
            "REMOTE_ADDR",
            "HTTP_USER_AGENT",
            "HTTP_X_FORWARDED_FOR",
            "nested",
            "note",
            "summary",
        ):
            self.assertNotIn(forbidden, closed)

        # Sanitize défensif : littéraux / texte libre / clé IP.
        cleaned = sanitize_audit_detail(dirty)
        blob = str(cleaned).casefold()
        self.assertNotIn("2001:db8", blob)
        self.assertNotIn("198.51.100", blob)
        self.assertNotIn("mozilla", blob)
        self.assertNotIn("203.0.113", blob)

        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        event = record_account_audit(
            actor=admin_user,
            action="probe_r2",
            target_user=admin_user,
            success=True,
            reason="ok",
            detail=dirty,
        )
        event_db = AuditEvent.objects.get(pk=event.pk)
        stored = event_db.detail
        stored_blob = str(stored).casefold()
        self.assertNotIn("2001:db8", stored_blob)
        self.assertNotIn("198.51.100", stored_blob)
        self.assertNotIn("mozilla", stored_blob)
        self.assertNotIn("evilagent", stored_blob)
        self.assertNotIn("203.0.113", stored_blob)
        self.assertNotIn("should-drop", stored_blob)
        self.assertEqual(stored.get("role"), "editor")
        self.assertEqual(stored.get("access_mode"), "temp_password")
        self.assertEqual(stored.get("sessions_deleted"), 2)
        self.assertEqual(stored.get("action"), "probe_r2")
        self.assertIs(stored.get("success"), True)
        self.assertEqual(stored.get("reason"), "ok")
        self.assertEqual(stored.get("target_user_id"), admin_user.pk)
        # Métadonnée métier sûre conservée ; rien d'imbrigué libre.
        self.assertNotIn("nested", stored)

    # --- R2-02 invitations ---

    def test_r2_02_reset_to_temp_password_invalidates_prior_invite(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        invite = create_account(
            role=UserProfile.Role.EDITOR,
            actor=admin_user,
            is_test_identity=True,
            use_invite_token=True,
        )
        target = get_user_model().objects.get(pk=invite.user_id)
        old_token = invite.invite_token
        self.assertTrue(old_token)

        reset = reset_access(target_user=target, actor=admin_user, use_invite_token=False)
        self.assertTrue(reset.temporary_password)
        self.assertIsNone(reset.invite_token)
        target.refresh_from_db()
        self.assertTrue(target.dv_profile.must_change_password)
        self.assertTrue(target.check_password(reset.temporary_password))

        with self.assertRaises(ValidationError):
            redeem_invite_token(raw_token=old_token, new_password="AncienJetonNeDoitPas-99!")
        target.refresh_from_db()
        self.assertTrue(target.check_password(reset.temporary_password))
        self.assertFalse(target.check_password("AncienJetonNeDoitPas-99!"))

        # Nouvel accès par MDP temporaire OK.
        client = Client()
        login = client.post(
            reverse("login"),
            {"username": reset.login_pseudonym, "password": reset.temporary_password},
            **PRIVATE_HEADER,
        )
        self.assertEqual(login.status_code, 302)

    def test_r2_02_reset_to_new_invite_and_lifecycle(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        invite = create_account(
            role=UserProfile.Role.EDITOR,
            actor=admin_user,
            is_test_identity=True,
            use_invite_token=True,
        )
        target = get_user_model().objects.get(pk=invite.user_id)
        old_token = invite.invite_token

        reset = reset_access(target_user=target, actor=admin_user, use_invite_token=True)
        self.assertTrue(reset.invite_token)
        self.assertNotEqual(reset.invite_token, old_token)
        with self.assertRaises(ValidationError):
            redeem_invite_token(raw_token=old_token, new_password="AncienJetonRefuse-11!")

        # Usage unique.
        user = redeem_invite_token(
            raw_token=reset.invite_token, new_password="NouvelInviteFort-22!"
        )
        self.assertEqual(user.pk, target.pk)
        with self.assertRaises(ValidationError):
            redeem_invite_token(
                raw_token=reset.invite_token, new_password="ReuseInviteRefuse-33!"
            )

        # Jeton expiré.
        fresh = reset_access(target_user=target, actor=admin_user, use_invite_token=True)
        token_row = AccountInviteToken.objects.get(
            token_digest__isnull=False, used_at__isnull=True, user=target
        )
        token_row.expires_at = timezone.now() - timedelta(seconds=1)
        token_row.save(update_fields=["expires_at"])
        with self.assertRaises(ValidationError):
            redeem_invite_token(raw_token=fresh.invite_token, new_password="ExpireRefuse-44!")

        # Révocation puis réactivation : ancien moyen d'activation inutilisable.
        again = reset_access(target_user=target, actor=admin_user, use_invite_token=True)
        set_account_status(
            target_user=target,
            actor=admin_user,
            status=UserProfile.AccountStatus.REVOKED,
            reason="revoked",
        )
        open_before = AccountInviteToken.objects.filter(
            user=target, used_at__isnull=True
        ).count()
        self.assertEqual(open_before, 0)
        set_account_status(
            target_user=target,
            actor=admin_user,
            status=UserProfile.AccountStatus.ACTIVE,
            reason="reactivated",
        )
        with self.assertRaises(ValidationError):
            redeem_invite_token(raw_token=again.invite_token, new_password="ApresRevocation-55!")

    # --- R2-03 throttle fenêtre ---

    def test_r2_03_global_throttle_time_window(self):
        LoginThrottle.objects.all().delete()
        factory = RequestFactory()
        pseudo_a = "compte-fenetre-a"
        pseudo_b = "compte-fenetre-b"
        now = timezone.now()

        def _fail(username: str):
            request = factory.post("/connexion/", {"username": username, "password": "x"})
            record_login_failure(request)

        # Remplir le bucket global jusqu'au seuil → verrou.
        with mock.patch("atelier.services.login_throttle.timezone.now", return_value=now):
            for i in range(GLOBAL_FAILURE_LIMIT):
                _fail(f"filler-{i}")
            locked_req = factory.post("/connexion/", {"username": pseudo_a, "password": "x"})
            self.assertTrue(is_login_throttled(locked_req))
            global_row = LoginThrottle.objects.get(key_digest=global_throttle_digest())
            self.assertIsNotNone(global_row.locked_until)
            self.assertGreaterEqual(global_row.failures, GLOBAL_FAILURE_LIMIT)

        # Après expiration du verrou global : premier échec nouvelle fenêtre ≠ verrou immédiat.
        after_lock = now + timedelta(minutes=GLOBAL_LOCK_MINUTES + 1)
        reset_ephemeral_login_state()
        with mock.patch(
            "atelier.services.login_throttle.timezone.now", return_value=after_lock
        ):
            open_req = factory.post("/connexion/", {"username": pseudo_a, "password": "x"})
            self.assertFalse(is_login_throttled(open_req))
            _fail(pseudo_a)
            global_row.refresh_from_db()
            self.assertEqual(global_row.failures, 1)
            self.assertTrue(
                global_row.locked_until is None or global_row.locked_until <= after_lock
            )
            self.assertFalse(is_login_throttled(open_req))

            # Autre compte OK après expiration.
            other = factory.post("/connexion/", {"username": pseudo_b, "password": "x"})
            self.assertFalse(is_login_throttled(other))
            _fail(pseudo_b)
            self.assertFalse(is_login_throttled(other))

        # Échecs espacés hors fenêtre : pas de cumul lifetime.
        LoginThrottle.objects.all().delete()
        t0 = timezone.now()
        with mock.patch("atelier.services.login_throttle.timezone.now", return_value=t0):
            for _ in range(3):
                _fail(pseudo_a)
            account_row = LoginThrottle.objects.get(
                key_digest=account_throttle_digest(pseudo_a)
            )
            self.assertEqual(account_row.failures, 3)

        later = t0 + timedelta(
            minutes=max(ACCOUNT_WINDOW_MINUTES, GLOBAL_WINDOW_MINUTES) + 1
        )
        with mock.patch("atelier.services.login_throttle.timezone.now", return_value=later):
            _fail(pseudo_a)
            account_row.refresh_from_db()
            self.assertEqual(account_row.failures, 1)
            global_row = LoginThrottle.objects.get(key_digest=global_throttle_digest())
            self.assertEqual(global_row.failures, 1)

        digests = set(LoginThrottle.objects.values_list("key_digest", flat=True))
        self.assertIn(account_throttle_digest(pseudo_a), digests)
        self.assertIn(global_throttle_digest(), digests)
        assert_no_network_material_persisted(
            digests=digests, remotes=["198.51.100.1"], user_agents=["Mozilla/5.0"]
        )

        # Seuil compte inchangé.
        LoginThrottle.objects.all().delete()
        t1 = timezone.now()
        with mock.patch("atelier.services.login_throttle.timezone.now", return_value=t1):
            for _ in range(ACCOUNT_FAILURE_LIMIT):
                _fail(pseudo_a)
            req = factory.post("/connexion/", {"username": pseudo_a, "password": "x"})
            self.assertTrue(is_login_throttled(req))
            clear_login_throttle(req)
            self.assertFalse(is_login_throttled(req))

    # --- R2-04 pseudonymes déterministes ---

    def test_r2_04_pseudonym_deterministic_regeneration_exhaustion_uniqueness(self):
        self.assertEqual(PSEUDONYM_COMPONENT_COUNTS, (18, 16, 18))
        self.assertEqual(PSEUDONYM_SPACE_SIZE, 18 * 16 * 18)
        self.assertEqual(PSEUDONYM_SPACE_SIZE, 5184)

        occupied = "Brume-Cedre-Aigle"
        user_model = get_user_model()
        user_model.objects.create(username=occupied)
        UserProfile.objects.create(
            user=user_model.objects.get(username=occupied),
            role=UserProfile.Role.EDITOR,
            login_pseudonym=occupied,
            public_attribution_label=occupied,
            is_test_identity=True,
        )

        # Candidat enregistré imposé → régénération vers un libre.
        sequence = [
            "Brume",
            "Cedre",
            "Aigle",  # occupé
            "Pluie",
            "Pin",
            "Lynx",  # libre
        ]
        with mock.patch("atelier.services.accounts.secrets.choice", side_effect=sequence):
            generated = generate_pseudonym()
        self.assertEqual(generated, "Pluie-Pin-Lynx")
        self.assertRegex(generated, _PSEUDONYM_RE)

        # Borne d'épuisement : toujours le même candidat occupé.
        stuck = ["Brume", "Cedre", "Aigle"] * 10
        with mock.patch("atelier.services.accounts.secrets.choice", side_effect=stuck):
            with self.assertRaises(ValidationError) as ctx:
                generate_pseudonym(max_attempts=3)
        self.assertIn("pseudonym_generation_exhausted", ctx.exception.messages)
        # Unicité des comptes créés + 3 composantes lexicales.
        created = [
            create_account(role=UserProfile.Role.EDITOR, is_test_identity=True)
            for _ in range(6)
        ]
        pseudos = {c.login_pseudonym for c in created}
        self.assertEqual(len(pseudos), 6)
        for pseudo in pseudos:
            self.assertRegex(pseudo, _PSEUDONYM_RE)
            parts = pseudo.split("-")
            self.assertEqual(len(parts), 3)
            for part in parts:
                self.assertTrue(part.isalpha())
                self.assertFalse(any(ch.isdigit() for ch in part))

        # Régénération admin : uid inchangé, nouvel identifiant libre.
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        target = get_user_model().objects.get(pk=created[0].user_id)
        uid = target.dv_profile.internal_uid
        new_name = regenerate_login_pseudonym(target_user=target, actor=admin_user)
        target.refresh_from_db()
        self.assertEqual(target.dv_profile.internal_uid, uid)
        self.assertEqual(target.pk, created[0].user_id)
        self.assertNotEqual(new_name, created[0].login_pseudonym)

    # --- Cycle de vie / preuves ---

    def test_r2_lifecycle_suspended_login_and_admin_surfaces(self):
        admin = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        admin_user = get_user_model().objects.get(pk=admin.user_id)
        admin_user.set_password("AdminCycleR2-99!")
        admin_user.dv_profile.must_change_password = False
        admin_user.dv_profile.save()
        admin_user.save()

        editor = create_account(
            role=UserProfile.Role.EDITOR, actor=admin_user, is_test_identity=True
        )
        editor_user = get_user_model().objects.get(pk=editor.user_id)
        editor_user.set_password("EditorCycleR2-99!")
        editor_user.dv_profile.must_change_password = False
        editor_user.dv_profile.save()
        editor_user.save()

        set_account_status(
            target_user=editor_user,
            actor=admin_user,
            status=UserProfile.AccountStatus.SUSPENDED,
            reason="suspended",
        )
        client = Client()
        refused = client.post(
            reverse("login"),
            {"username": editor.login_pseudonym, "password": "EditorCycleR2-99!"},
            **PRIVATE_HEADER,
        )
        self.assertEqual(refused.status_code, 403)

        # Attribution user_id après régénération pseudo.
        set_account_status(
            target_user=editor_user,
            actor=admin_user,
            status=UserProfile.AccountStatus.ACTIVE,
            reason="active",
        )
        old_pk = editor_user.pk
        new_pseudo = regenerate_login_pseudonym(target_user=editor_user, actor=admin_user)
        editor_user.refresh_from_db()
        self.assertEqual(editor_user.pk, old_pk)
        self.assertEqual(editor_user.username, new_pseudo)

        # Surfaces admin refusées au non-admin (403, pas hasattr).
        editor_user.set_password("EditorCycleR2-99!")
        editor_user.dv_profile.must_change_password = False
        editor_user.dv_profile.save()
        editor_user.save()
        client.force_login(editor_user)
        denied = client.get(reverse("accounts_list"), **PRIVATE_HEADER)
        self.assertEqual(denied.status_code, 403)
        client.force_login(admin_user)
        ok = client.get(reverse("accounts_list"), **PRIVATE_HEADER)
        self.assertEqual(ok.status_code, 200)
