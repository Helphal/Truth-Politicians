"""Livraison événement Outil → WordPress Local B (loopback, hors test runner Django)."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atelier.models import EditorialDossier, EditorialRevision, OutboxEvent
from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.services.editorial_feed import accept_confirmation, verify_confirmation
from atelier.services.full_review import approval_bytes, build_review_bundle
from atelier.services.publication import packet_for_event
from atelier.services.wordpress_loopback import (
    fetch_loopback_html,
    fetch_rest_loopback,
    post_packet_loopback,
    post_rest_loopback,
)
from atelier.tests.racc001_php import SITE_RACC001_EVENT_SIG_VERIFY, SITE_RACC001_REVIEW_BUNDLE_VALIDATE, php_argv

LOCAL_B_EVENT_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/events"
LOCAL_B_REVIEW_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/reviews"
LOCAL_B_CONFIRMATION_PATH = "http://127.0.0.1:10040/wp-json/dv/v2/publication-confirmation/{event_id}"
LOCAL_B_COMPARER_HTML = "http://127.0.0.1:10040/comparer/"
DEFAULT_LOCAL_B_KEYS = Path(
    r"C:\Users\EXAMPLE_USER\AppData\Local\TruthPoliticians\wordpress-local-b\keys\dv-local-001-test-keys.json"
)


def _keys_meta() -> dict:
    keys_path = Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", str(DEFAULT_LOCAL_B_KEYS)))
    return json.loads(keys_path.read_text(encoding="utf-8"))


def _review_signing_key() -> Ed25519PrivateKey:
    material = _keys_meta()
    raw = base64.b64decode(material["review_secret_base64"])
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def post_review_to_wp(dossier: EditorialDossier, revision: EditorialRevision, event: OutboxEvent) -> dict:
    bundle = build_review_bundle(revision, environment="test", destination="dv-site:test")
    review_key = _review_signing_key()
    keys_meta = _keys_meta()
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
    body = {"bundle": bundle, "proof": proof}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as review_tmp:
        review_tmp.write(json.dumps(body, ensure_ascii=False))
        review_path = review_tmp.name
    proc = subprocess.run(
        php_argv(str(SITE_RACC001_REVIEW_BUNDLE_VALIDATE), review_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=os.environ.copy(),
    )
    Path(review_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout + proc.stderr)
    return post_rest_loopback(LOCAL_B_REVIEW_PATH, body, idempotency_key=sha256_digest(bundle))


def post_event_to_wp(event: OutboxEvent) -> dict:
    packet = packet_for_event(event)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        tmp.write(canonical_bytes(packet).decode("utf-8"))
        tmp_path = tmp.name
    proc = subprocess.run(
        php_argv(str(SITE_RACC001_EVENT_SIG_VERIFY), tmp_path),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    Path(tmp_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout + proc.stderr)
    return post_packet_loopback(packet)


def fetch_wp_confirmation(event: OutboxEvent) -> dict:
    return fetch_rest_loopback(LOCAL_B_CONFIRMATION_PATH.format(event_id=str(event.event_id)))


def accept_wp_confirmation(event: OutboxEvent, conf: dict) -> None:
    stmt = conf["statement"]
    verify_confirmation(event, stmt, conf["signature"])
    accept_confirmation(event, stmt, conf["signature"])


def fetch_comparer_html(record_id: str) -> tuple[int, str]:
    from urllib.parse import quote

    url = f"{LOCAL_B_COMPARER_HTML}?dossier={quote(record_id, safe='')}"
    return fetch_loopback_html(url)


def deliver_queued_event(dossier: EditorialDossier, revision: EditorialRevision, event: OutboxEvent) -> dict:
    review_resp: dict = {"_skipped": True, "_loopback_http_status": None}
    if dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE:
        review_resp = post_review_to_wp(dossier, revision, event)
    event_resp = post_event_to_wp(event)
    conf = fetch_wp_confirmation(event)
    accept_wp_confirmation(event, conf)
    return {
        "review_http": review_resp.get("_loopback_http_status"),
        "event_http": event_resp.get("_loopback_http_status"),
        "confirmation_status": conf.get("statement", {}).get("status"),
        "public_url": conf.get("statement", {}).get("public_url"),
    }
