"""Helpers HTTP Local B partagés (RECETTE R5/R6 live)."""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
import tempfile
from pathlib import Path
from datetime import date
from urllib.parse import quote, urlencode
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from atelier.models import (
    Affiliation,
    Comparison,
    ComparisonAct,
    Declaration,
    EvidencePassage,
    Organization,
    Person,
    VoteObservation,
)
from atelier.services.canonical import canonical_bytes, sha256_digest
from atelier.services.editorial_feed import accept_confirmation, verify_confirmation
from atelier.services.full_review import approval_bytes, build_review_bundle
from atelier.services.publication import authorize_and_queue, packet_for_event, queue_suspension
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.services.wordpress import WordPressUnavailable
from atelier.services.wordpress_loopback import (
    fetch_loopback_html,
    fetch_rest_loopback,
    post_packet_loopback,
    post_rest_loopback,
    resolve_loopback_endpoint,
    _loopback_auth_headers,
    _loopback_http_post,
)
from atelier.tests.racc001_php import SITE_RACC001_EVENT_SIG_VERIFY, SITE_RACC001_REVIEW_BUNDLE_VALIDATE, php_argv
from atelier.services.detached_review import accept_detached_review
from atelier.tests.test_racc001_r5_local_loopback import (
    DEFAULT_LOCAL_B_KEYS,
    LOCAL_B_CONFIRMATION_PATH,
    LOCAL_B_EVENT_PATH,
    LOCAL_B_REVIEW_PATH,
    _local_b_event_keys,
    _local_b_review_signing_key,
    _local_b_site_confirmation_public_keys,
    _proof_step,
)

LOCAL_B_COMPARER_HTML = "http://127.0.0.1:10040/comparer/"
LOCAL_B_COMPARAISONS_HTML = "http://127.0.0.1:10040/comparaisons/"


class LocalBLiveSupport:
    sequence = 0

    def _reset_live_run_identity(self):
        type(self).sequence = 0
        os.environ["DV_RACC001_RUN_ID"] = secrets.token_hex(8)
        os.environ["DV_RACC001_RUN_NONCE"] = secrets.token_hex(4)

    def _detached_approve(self, dossier, revision, user, keys):
        bundle = build_review_bundle(revision, environment="test", destination="dv-site:test")
        key = Ed25519PrivateKey.generate()
        review_keys = {**keys, "test-racc4": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
        statement = {
            "algorithm": "ed25519",
            "key_id": "test-racc4",
            "bundle_sha256": sha256_digest(bundle),
            "record_id": dossier.stable_id,
            "record_revision": revision.revision,
            "decision": "approve",
            "reviewed_at": timezone.now().isoformat(),
        }
        proof = {
            "statement": statement,
            "signature": base64.b64encode(key.sign(approval_bytes(bundle, statement))).decode(),
        }
        holder = str(user.dv_profile.internal_uid)
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=review_keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
        ):
            accept_detached_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                bundle=bundle,
                proof=proof,
                user=user,
                environment="test",
                destination="dv-site:test",
            )
        return review_keys

    def unique(self, prefix: str) -> str:
        run = os.environ.get("DV_RACC001_RUN_ID", "LOCAL")
        nonce = os.environ.get("DV_RACC001_RUN_NONCE", "")
        if not nonce:
            nonce = os.urandom(4).hex()
            os.environ["DV_RACC001_RUN_NONCE"] = nonce
        self.sequence += 1
        method = getattr(self, "_testMethodName", "case")
        return f"{prefix}:{run}:{nonce}:{method}:{self.sequence}"

    def _loopback_patch(self):
        return patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_LOOPBACK_TEST": "1",
                "DV_WORDPRESS_LOOPBACK_ENDPOINT": LOCAL_B_EVENT_PATH,
                "DV_WORDPRESS_LOOPBACK_CONNECT_PORTS": "10040",
                "DV_LOCAL_B_WP_USER": os.environ.get("DV_LOCAL_B_WP_USER", ""),
                "DV_LOCAL_B_WP_PASSWORD": os.environ.get("DV_LOCAL_B_WP_PASSWORD", ""),
            },
            clear=False,
        )

    def _post_review_wp(self, dossier, revision, event) -> dict:
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
        )
        Path(review_path).unlink(missing_ok=True)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        idem = sha256_digest(bundle)
        for attempt in range(5):
            try:
                return post_rest_loopback(LOCAL_B_REVIEW_PATH, body, idempotency_key=idem)
            except WordPressUnavailable as error:
                if "429" not in str(error) or attempt >= 4:
                    raise
                time.sleep(2 * (attempt + 1))
        raise WordPressUnavailable("loopback_review_retry_exhausted")

    def _post_event_wp(self, event) -> dict:
        packet = packet_for_event(event)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            tmp.write(canonical_bytes(packet).decode("utf-8"))
            tmp_path = tmp.name
        env = os.environ.copy()
        proc = subprocess.run(
            php_argv(str(SITE_RACC001_EVENT_SIG_VERIFY), tmp_path),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        return post_packet_loopback(packet)

    def _confirmation_get(self, event_id: str):
        return fetch_rest_loopback(LOCAL_B_CONFIRMATION_PATH.format(event_id=event_id))

    def _publish_cycle(self, dossier, revision, user, keys) -> tuple:
        holder = str(user.dv_profile.internal_uid)
        with override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_event_keys(),
            DV_SITE_PUBLICATION_PUBLIC_KEYS=_local_b_site_confirmation_public_keys(),
        ):
            event = authorize_and_queue(revision, "test")
            self._post_review_wp(dossier, revision, event)
            self._post_event_wp(event)
            conf = self._confirmation_get(str(event.event_id))
            stmt = conf["statement"]
            verify_confirmation(event, stmt, conf["signature"])
            accept_confirmation(event, stmt, conf["signature"])
            return event, conf

    def _comparaisons_grid_html(self, query: dict[str, str] | None = None) -> str:
        """Grille /comparaisons/ (filtres année, acteur, etc.)."""
        url = LOCAL_B_COMPARAISONS_HTML
        if query:
            url = f"{url}?{urlencode(query)}"
        status, html = fetch_loopback_html(url)
        self.assertEqual(status, 200, html[:500])
        return html

    def _make_personal_date_chain(
        self,
        *,
        precision: str,
        marker: str,
        occurred_on: date | None = None,
        occurred_on_state: str | None = None,
        interval_end: date | None = None,
        publication_date: date | None = None,
        coverage: dict | None = None,
    ):
        from atelier.models import DataState

        declaration, _, scrutiny, _ = self.make_chain(
            person=True,
            declaration_date=occurred_on or date(2026, 1, 15),
        )
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.date_precision = precision
        if occurred_on_state is not None:
            declaration.occurred_on_state = occurred_on_state
            if occurred_on_state == DataState.UNKNOWN:
                declaration.occurred_on = None
        if interval_end is not None:
            declaration.occurred_interval_end = interval_end
        if publication_date is not None:
            declaration.published_on = publication_date
        declaration.exact_quote = f"RECETTE-05-{marker} {declaration.exact_quote}"
        declaration.save(
            update_fields=[
                "position_scope",
                "date_precision",
                "occurred_on_state",
                "occurred_on",
                "occurred_interval_end",
                "published_on",
                "exact_quote",
                "updated_at",
            ]
        )
        passage = declaration.source_passage
        passage.exact_text = declaration.exact_quote
        passage.save(update_fields=["exact_text"])
        stable_id = f"RECETTE05-{secrets.token_hex(8)}"
        dossier, revision, _ = self._revision_with_comparison(
            declaration,
            scrutiny,
            coverage=coverage,
            stable_id=stable_id,
        )
        return dossier, revision, declaration

    def _comparer_html(self, record_id: str, *, allowed_status: frozenset[int] | None = None) -> str:
        url = f"{LOCAL_B_COMPARER_HTML}?dossier={quote(record_id, safe='')}"
        status, html = fetch_loopback_html(url)
        expected = allowed_status or frozenset({200})
        self.assertIn(status, expected, html[:500])
        return html

    def _post_rest_status(
        self,
        endpoint: str,
        body: dict,
        *,
        idempotency_key: str,
        auth: bool = True,
    ) -> tuple[int, str]:
        target = resolve_loopback_endpoint(endpoint)
        payload = canonical_bytes(body)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
            "Host": target.host_header,
            "User-Agent": "TruthPoliticians-loopback-test/0.1",
            "Connection": "close",
        }
        if auth:
            headers.update(_loopback_auth_headers())
        status, raw = _loopback_http_post(target, body=payload, headers=headers, timeout=20)
        return status, raw.decode("utf-8", errors="replace")

    def _quote_projection_count(self, record_id: str, quote: str) -> int:
        html = self._comparer_html(record_id)
        return html.count(quote)

    def _assert_unpublished(self, record_id: str, quote: str) -> None:
        html = self._comparer_html(record_id, allowed_status=frozenset({200, 404}))
        if quote:
            self.assertNotIn(quote, html)
        self.assertTrue(
            "Aucun dossier publié avec cet identifiant exact" in html
            or "Dossier indisponible" in html
            or quote not in html,
            html[:400],
        )

    def _make_collective_month_chain(self):
        from atelier.tests.racc001_php import RACC001_PRESS_PUBLIC_URL

        declaration_document = self.make_document(source_id=self.unique("PRESS"))
        passage = EvidencePassage.objects.create(
            document=declaration_document,
            exact_text="Notre groupe soutient la mesure en septembre.",
            locator="p1",
            public_url=RACC001_PRESS_PUBLIC_URL,
        )
        person = Person.objects.create(stable_id=self.unique("PERSON"), display_name="Porte-parole RECETTE")
        group = Organization.objects.create(
            stable_id=self.unique("ORG"),
            name="Groupe RECETTE-05",
            kind=Organization.Kind.PARLIAMENTARY_GROUP,
        )
        declaration = Declaration.objects.create(
            stable_id=self.unique("DECL"),
            speaker_person=person,
            speaker_organization=group,
            speaker_label=person.display_name,
            capacity="porte-parole",
            exact_quote=passage.exact_text,
            occurred_on=__import__("datetime").date(2026, 9, 1),
            date_precision=Declaration.DatePrecision.MONTH,
            position_scope=Declaration.PositionScope.COLLECTIVE,
            source_passage=passage,
        )
        scrutiny = self.make_scrutiny(self.make_document(source_id=self.unique("OFFICIAL")))
        scrutiny.occurred_on = date(2026, 9, 15)
        scrutiny.save(update_fields=["occurred_on", "updated_at"])
        ally = Person.objects.create(
            stable_id=self.unique("PERSON-ALLY"),
            display_name="Élue affiliée RECETTE",
        )
        conflicted = Person.objects.create(
            stable_id=self.unique("PERSON-CF"),
            display_name="Élue en conflit RECETTE",
        )
        missing = Person.objects.create(
            stable_id=self.unique("PERSON-MISS"),
            display_name="Élue sans vote RECETTE",
        )
        affiliation_starts = date(2026, 1, 1)
        for member in (person, ally, conflicted, missing):
            Affiliation.objects.create(
                person=member,
                organization=group,
                status="official_group_mandate",
                starts_on=affiliation_starts,
                ends_on=None,
                observed_on=date(2026, 6, 1),
                source_document=declaration_document,
            )
        vote_doc = self.make_document(source_id=self.unique("VOTE-BASE"))
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=ally,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=vote_doc,
        )
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=conflicted,
            source_category="Pour",
            normalized_category=VoteObservation.Category.FOR,
            mapping_rule="TEST-EXACT",
            source_document=vote_doc,
        )
        alt_vote_doc = self.make_document(source_id=self.unique("VOTE-CF"))
        VoteObservation.objects.create(
            scrutiny=scrutiny,
            person=conflicted,
            source_category="Contre",
            normalized_category=VoteObservation.Category.AGAINST,
            mapping_rule="TEST-EXACT",
            source_document=alt_vote_doc,
        )
        stable_id = f"RECETTE05-MONTH-{secrets.token_hex(8)}"
        dossier, revision, _ = self._revision_with_comparison(
            declaration,
            scrutiny,
            stable_id=stable_id,
        )
        return dossier, revision, declaration, group
