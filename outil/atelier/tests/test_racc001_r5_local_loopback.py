"""R5 — sonde HTTP réelle Local B (2 tests, hors banc unitaire principal)."""
from __future__ import annotations

import unittest

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from atelier.models import Declaration
from atelier.services.canonical import sha256_digest
from atelier.services.full_review import approval_bytes, build_review_bundle
from atelier.services.publication import authorize_and_queue, packet_for_event
from atelier.services.wordpress import WordPressUnavailable
from atelier.services.editorial_feed import accept_confirmation, verify_confirmation
from atelier.services.loopback_fault_injector import fault_server_session, pop_request_log
from atelier.services.wordpress_loopback import (
    enqueue_loopback_faults,
    fetch_rest_loopback,
    post_packet_loopback,
    post_rest_loopback,
    reset_loopback_fault_queue,
    resolve_loopback_endpoint,
)
from atelier.services.wordpress_loopback import _loopback_http_post  # test-only: preuve socket timeout réel
from atelier.services.canonical import canonical_bytes
from atelier.tests.racc001_php import (
    SITE_RACC001_EVENT_SIG_VERIFY,
    SITE_RACC001_REVIEW_BUNDLE_VALIDATE,
    SITE_RACC001_RECOVERY_BOUNDED,
    SITE_RACC001_SITE_CONFIRMATION_MATERIAL,
    php_argv,
)
from atelier.tests.test_racc001_r4_bilateral import Racc001R4BilateralTests


LOCAL_B_EVENT_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/events"
LOCAL_B_REVIEW_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/reviews"
LOCAL_B_CONFIRMATION_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/publication-confirmation/{event_id}"
DEFAULT_LOCAL_B_KEYS = Path(
    r"C:\Users\EXAMPLE_USER\AppData\Local\TruthPoliticians\wordpress-local-b\keys\dv-local-001-test-keys.json"
)


def _local_b_event_keys() -> dict:
    raw = os.environ.get("DV_EVENT_TEST_EVENT_SECRET_BASE64", "").strip()
    if not raw:
        raise WordPressUnavailable("loopback_event_secret_missing")
    return {
        "test-v2-event": {
            "environment": "test",
            "destination": "dv-site:test",
            "purpose": "event",
            "active": True,
            "secret": base64.b64decode(raw),
        }
    }


def _local_b_transport_keys() -> dict:
    """Clés transport alignées sur le fichier Local B (événement + reçu)."""
    keys_path = Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS)))
    material = json.loads(keys_path.read_text(encoding="utf-8"))
    transport = material.get("transport") or {}
    keys: dict = {}
    for key_id, entry in transport.items():
        if not isinstance(entry, dict):
            continue
        secret_raw = entry.get("secret_base64", "")
        secret = base64.b64decode(secret_raw) if secret_raw else b""
        keys[key_id] = {
            "environment": entry.get("environment"),
            "destination": entry.get("destination"),
            "purpose": entry.get("usage"),
            "active": bool(entry.get("active")),
            "secret": secret,
        }
    if "test-v2-event" not in keys:
        keys.update(_local_b_event_keys())
    return keys


LOCAL_B_RECEIPT_KEY_ID = "test-v2-receipt"


def _local_b_site_confirmation_public_keys() -> dict:
    keys_path = Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS)))
    material = json.loads(keys_path.read_text(encoding="utf-8"))
    key_id = material["site_confirmation_key_id"]
    return {key_id: material["site_confirmation_public_base64"]}


def _local_b_review_signing_key() -> Ed25519PrivateKey:
    keys_path = Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS)))
    if not keys_path.is_file():
        raise WordPressUnavailable("loopback_review_keys_missing")
    material = json.loads(keys_path.read_text(encoding="utf-8"))
    raw = base64.b64decode(material["review_secret_base64"])
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def _proof_step(label: str, detail: str = "") -> None:
    path = os.environ.get("DV_RACC001_PROOF_STAMP", "").strip()
    if not path:
        path = os.environ.get("DV_RACC001_R5_PROOF_APPEND", "").strip()
    if not path:
        return
    line = f"{timezone.now().isoformat()} {label}"
    if detail:
        line = f"{line} {detail}"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


@unittest.skipUnless(os.environ.get("DV_RACC001_LIVE_WORDPRESS") == "1",
    "Integration WordPress Local B non demandee : banc reel et cles SYNTH requis")
class Racc001LocalLoopbackLiveTests(Racc001R4BilateralTests):
    """Hérite des helpers R4/corpus ; seuls les tests ci-dessous sont lancés par run-r5-loopback-probe.ps1."""

    def unique(self, prefix: str) -> str:
        run = os.environ.get("DV_RACC001_RUN_ID", "LOCAL")
        self.sequence += 1
        method = getattr(self, "_testMethodName", "case")
        return f"{prefix}:{run}:{method}:{self.sequence}"

    def test_recovery_bounded_reconcile_php(self):
        proc = subprocess.run(
            php_argv(str(SITE_RACC001_RECOVERY_BOUNDED)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload.get("ok"), payload)

    def test_site_confirmation_signing_material_php(self):
        proc = subprocess.run(
            php_argv(str(SITE_RACC001_SITE_CONFIRMATION_MATERIAL)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload.get("ok"), payload)

    def test_resolve_local_b_port_10040(self):
        target = resolve_loopback_endpoint(LOCAL_B_EVENT_PATH)
        self.assertEqual(target.port, 10040)
        self.assertEqual(target.host_header, "truth-politicians.test:10040")

    @override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
    def test_live_post_to_local_b_wordpress(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_event_keys(),
        ), patch.dict(os.environ, {
            "DV_WORDPRESS_LOOPBACK_TEST": "1",
            "DV_WORDPRESS_LOOPBACK_ENDPOINT": LOCAL_B_EVENT_PATH,
            "DV_WORDPRESS_LOOPBACK_CONNECT_PORTS": "10040",
            "DV_LOCAL_B_WP_USER": os.environ.get("DV_LOCAL_B_WP_USER", ""),
            "DV_LOCAL_B_WP_PASSWORD": os.environ.get("DV_LOCAL_B_WP_PASSWORD", ""),
        }, clear=False):
            event = authorize_and_queue(revision, "test")
            bundle = build_review_bundle(revision, environment="test", destination="dv-site:test")
            review_key = _local_b_review_signing_key()
            keys_meta = json.loads(
                Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS))).read_text(encoding="utf-8")
            )
            review_date = event.payload["authorization"]["public_review"]["date"]
            statement = {
                "algorithm": "ed25519",
                "key_id": keys_meta["review_key_id"],
                "bundle_sha256": sha256_digest(bundle),
                "record_id": dossier.stable_id,
                "record_revision": revision.revision,
                "decision": "approve",
                "reviewed_at": f"{review_date}T12:00:00+00:00",
            }
            proof = {
                "statement": statement,
                "signature": base64.b64encode(review_key.sign(approval_bytes(bundle, statement))).decode(),
            }
            review_body = {"bundle": bundle, "proof": proof}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as review_tmp:
                review_tmp.write(json.dumps(review_body, ensure_ascii=False))
                review_path = review_tmp.name
            proc_review = subprocess.run(
                php_argv(str(SITE_RACC001_REVIEW_BUNDLE_VALIDATE), review_path),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={
                    **os.environ,
                    "DV_LOCAL_B_TEST_KEYS_JSON": os.environ.get(
                        "DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS)
                    ),
                },
            )
            Path(review_path).unlink(missing_ok=True)
            self.assertEqual(proc_review.returncode, 0, proc_review.stdout + proc_review.stderr)
            review_resp = post_rest_loopback(
                LOCAL_B_REVIEW_PATH,
                review_body,
                idempotency_key=sha256_digest(bundle),
            )
            _proof_step(
                "review_received",
                f"http={review_resp.get('_loopback_http_status')} duplicate={review_resp.get('duplicate', False)}",
            )
            packet = packet_for_event(event)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                tmp.write(canonical_bytes(packet).decode("utf-8"))
                tmp_path = tmp.name
            env = os.environ.copy()
            env.setdefault("DV_EVENT_TEST_EVENT_SECRET_BASE64", "")
            proc = subprocess.run(
                php_argv(str(SITE_RACC001_EVENT_SIG_VERIFY), tmp_path),
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
            )
            Path(tmp_path).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            site_pub_keys = _local_b_site_confirmation_public_keys()
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=site_pub_keys):
                try:
                    body = post_packet_loopback(packet)
                except WordPressUnavailable as exc:
                    self.fail(f"local_b_http_failed:{exc}")
                _proof_step(
                    "event_applied",
                    f"event_id={event.event_id} http={body.get('_loopback_http_status')}",
                )
                conf = fetch_rest_loopback(LOCAL_B_CONFIRMATION_PATH.format(event_id=str(event.event_id)))
                _proof_step("confirmation_GET", f"event_id={event.event_id} http=200")
                self.assertIn("statement", conf)
                self.assertIn("signature", conf)
                stmt = conf["statement"]
                self.assertEqual(stmt["event_digest"], event.payload_digest)
                verify_confirmation(event, stmt, conf["signature"])
                accept_confirmation(event, stmt, conf["signature"])
                _proof_step("confirmation_accepted", f"event_id={event.event_id}")
                bad = dict(stmt)
                bad["event_digest"] = "0" * 64
                with self.assertRaises(ValidationError):
                    verify_confirmation(event, bad, conf["signature"])
                _proof_step("negative_wrong_digest", "rejected")
        self.assertIsInstance(body, dict)

    @override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
    def test_live_event_idempotent_replay(self):
        """Rejoue le même paquet événement (même event_id) — seul ce test refait un POST identique."""
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_event_keys(),
        ), patch.dict(os.environ, {
            "DV_WORDPRESS_LOOPBACK_TEST": "1",
            "DV_WORDPRESS_LOOPBACK_ENDPOINT": LOCAL_B_EVENT_PATH,
            "DV_WORDPRESS_LOOPBACK_CONNECT_PORTS": "10040",
            "DV_LOCAL_B_WP_USER": os.environ.get("DV_LOCAL_B_WP_USER", ""),
            "DV_LOCAL_B_WP_PASSWORD": os.environ.get("DV_LOCAL_B_WP_PASSWORD", ""),
        }, clear=False):
            event = authorize_and_queue(revision, "test")
            bundle = build_review_bundle(revision, environment="test", destination="dv-site:test")
            review_key = _local_b_review_signing_key()
            keys_meta = json.loads(
                Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS))).read_text(encoding="utf-8")
            )
            review_date = event.payload["authorization"]["public_review"]["date"]
            statement = {
                "algorithm": "ed25519",
                "key_id": keys_meta["review_key_id"],
                "bundle_sha256": sha256_digest(bundle),
                "record_id": dossier.stable_id,
                "record_revision": revision.revision,
                "decision": "approve",
                "reviewed_at": f"{review_date}T12:00:00+00:00",
            }
            proof = {
                "statement": statement,
                "signature": base64.b64encode(review_key.sign(approval_bytes(bundle, statement))).decode(),
            }
            post_rest_loopback(
                LOCAL_B_REVIEW_PATH,
                {"bundle": bundle, "proof": proof},
                idempotency_key=sha256_digest(bundle),
            )
            packet = packet_for_event(event)
            first = post_packet_loopback(packet)
            second = post_packet_loopback(packet)
            self.assertEqual(first.get("_loopback_http_status"), 201)
            self.assertEqual(second.get("_loopback_http_status"), 200)
            _proof_step("idempotent_replay", f"event_id={event.event_id} http=201,200")

    def test_loopback_fault_http_timeout_real_socket(self):
        target = resolve_loopback_endpoint(LOCAL_B_EVENT_PATH)
        env = {
            "DV_WORDPRESS_LOOPBACK_TEST": "1",
            "DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR": "1",
            "DV_WORDPRESS_LOOPBACK_FAULT_TIMEOUT_DELAY": "3",
            "DV_WORDPRESS_LOOPBACK_CONNECT_PORTS": "10040,12789",
        }
        headers = {
            "Content-Type": "application/json",
            "Host": target.host_header,
            "Connection": "close",
        }
        with fault_server_session(), patch.dict(os.environ, env, clear=False):
            reset_loopback_fault_queue()
            enqueue_loopback_faults("http_timeout")
            with self.assertRaises(WordPressUnavailable) as exc:
                _loopback_http_post(target, body=b"{}", headers=headers, timeout=1.0)
            self.assertIn("loopback_timeout", str(exc.exception))
            hits = [line for line in pop_request_log() if "timeout" in line]
            self.assertTrue(hits, "injecteur timeout a reçu la requête HTTP")
