"""RECETTE-05/06/07/08/09/10 — HTTP réel Local B (tests isolés, sans héritage R4/R5 live)."""
from __future__ import annotations

import unittest

import base64
import json
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from datetime import date

from django.core.exceptions import ValidationError
from django.test import override_settings
from unittest.mock import patch

from atelier.models import DataState, Declaration, Organization, OutboxEvent, Person
from atelier.services.canonical import sha256_digest
from atelier.services.editorial_feed import accept_confirmation
from atelier.services.full_review import approval_bytes, build_review_bundle
from atelier.services.publication import authorize_and_queue, packet_for_event, queue_suspension, sign_packet
from atelier.services.loopback_fault_injector import fault_server_session, pop_request_log
from atelier.services.wordpress_loopback import (
    enqueue_loopback_faults,
    post_packet_loopback,
    reset_loopback_fault_queue,
)
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.services.wordpress import WordPressUnavailable, send_packet
from atelier.tests.racc001_local_b_live import LocalBLiveSupport
from atelier.tests.racc001_php import php_invocation_proof
from atelier.services.catalogue import SPEECH_VOTE_DERIVED_KEYS
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_racc001_corpus import Racc001CorpusTests
from atelier.tests.test_racc001_r5_local_loopback import (
    LOCAL_B_EVENT_PATH,
    LOCAL_B_REVIEW_PATH,
    LOCAL_B_RECEIPT_KEY_ID,
    _local_b_event_keys,
    _local_b_review_signing_key,
    _local_b_site_confirmation_public_keys,
    _local_b_transport_keys,
    _proof_step,
)


class _CorpusHarness(Racc001CorpusTests):
    __test__ = False


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
@unittest.skipUnless(os.environ.get("DV_RACC001_LIVE_WORDPRESS") == "1",
    "Integration WordPress Local B non demandee : banc reel et cles SYNTH requis")
class Racc001RecetteHttpLiveTests(LocalBLiveSupport, AtelierTestCase):
    """Cinq scénarios HTTP Local B uniquement (pas de tests corpus/R4/R5 découverts)."""

    def setUp(self):
        AtelierTestCase.setUp(self)
        self._reset_live_run_identity()
        self._corpus = _CorpusHarness()
        self._corpus.setUp()
        # Identifiants dossier WP stables par run/méthode (le corpus seul émet EDITORIAL:1…).
        self._corpus.unique = self.unique

    def make_chain(self, **kwargs):
        return self._corpus.make_chain(**kwargs)

    def _revision_with_comparison(self, *args, **kwargs):
        return self._corpus._revision_with_comparison(*args, **kwargs)

    def make_user(self, **kwargs):
        return self._corpus.make_user(**kwargs)

    def _live_settings(self, keys, holder):
        return override_settings(
            DV_DETACHED_REVIEW_PUBLIC_KEYS=keys,
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder,
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_transport_keys(),
            DV_RECEIPT_KEY_ID=LOCAL_B_RECEIPT_KEY_ID,
            DV_SITE_PUBLICATION_PUBLIC_KEYS=_local_b_site_confirmation_public_keys(),
        )

    def test_recette08_correction_http(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with self._loopback_patch(), self._live_settings(keys, holder):
            event1, conf1 = self._publish_cycle(dossier, revision, user, keys)
            quote_v1 = revision.public_payload["declaration_quote"]
            quote_v2 = quote_v1 + " [correction RECETTE-08]"
            declaration.exact_quote = quote_v2
            passage = declaration.source_passage
            passage.exact_text = quote_v2
            declaration.save(update_fields=["exact_quote", "updated_at"])
            passage.save(update_fields=["exact_text", "updated_at"])
            base = {k: v for k, v in revision.public_payload.items() if k not in SPEECH_VOTE_DERIVED_KEYS}
            base["short_answer"] = base["short_answer"] + " — révision corrigée RECETTE-08"
            revision2 = create_revision(
                dossier=dossier,
                public_payload=base,
                source_snapshot=revision.source_snapshot,
                title=base["title"],
                short_answer=base["short_answer"],
                main_caveat=base["main_caveat"],
                reasoning=base["reasoning"],
                result=base["result"],
                method_version=revision.method_version,
                change_summary="Correction RECETTE-08",
                comparison=revision.comparison,
            )
            record_controls(revision2, deterministic_control_results(revision2), environment="test")
            with self.assertRaises(ValidationError):
                accept_confirmation(event1, conf1["statement"], conf1["signature"])
            keys2 = self._detached_approve(dossier, revision2, user, keys)
            event2, conf2 = self._publish_cycle(dossier, revision2, user, keys2)
            self.assertEqual(conf2["statement"]["change_kind"], "correction")
            html = self._comparer_html(dossier.stable_id)
            self.assertEqual(revision2.public_payload["declaration_quote"], quote_v2)
            self.assertIn(quote_v2, html)
            self.assertIn("[correction RECETTE-08]", html)
            self.assertNotIn(f"<blockquote>{quote_v1}</blockquote>", html)
            self.assertIn("révision corrigée RECETTE-08", html)
            grid = self._comparaisons_grid_html()
            self.assertIn(revision2.public_payload["declaration_quote"], grid)
            self.assertIn(dossier.stable_id, grid)
            _proof_step("recette08_ok", f"{php_invocation_proof()} event2={event2.event_id}")

    def test_recette09_suspension_http(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        quote = revision.public_payload["declaration_quote"]
        with self._loopback_patch(), self._live_settings(keys, holder):
            event, conf = self._publish_cycle(dossier, revision, user, keys)
            self.assertIn(quote, self._comparer_html(dossier.stable_id))
            suspend = queue_suspension(dossier, "Suspension RECETTE-09.", environment="test")
            self._post_event_wp(suspend)
            with self.assertRaises(WordPressUnavailable) as ctx:
                self._confirmation_get(str(event.event_id))
            self.assertIn("404", str(ctx.exception))
            html = self._comparer_html(dossier.stable_id, allowed_status=frozenset({200, 404}))
            self.assertIn("Dossier indisponible", html)
            grid = self._comparaisons_grid_html()
            self.assertNotIn(dossier.stable_id, grid)
            with self.assertRaises(ValidationError):
                accept_confirmation(event, conf["statement"], conf["signature"])
            _proof_step("recette09_ok", f"record={dossier.stable_id}")

    def test_recette05_collective_month_ui_http(self):
        dossier, revision, declaration, group = self._make_collective_month_chain()
        self.assertEqual(revision.public_payload["compared_actor_scope"], "collective")
        self.assertEqual(revision.public_payload["qualified_declaration"]["precision"], "month")
        self.assertEqual(revision.public_payload["qualified_declaration"]["value"], "2026-09")
        self.assertEqual(revision.public_payload["declaration_date"], "2026-09")
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with self._loopback_patch(), self._live_settings(keys, holder):
            self._publish_cycle(dossier, revision, user, keys)
            html = self._comparer_html(dossier.stable_id)
            self.assertNotIn(
                "Aucun dossier publié avec cet identifiant exact",
                html,
                "comparer vide — publication ou record_id incorrect",
            )
            self.assertIn(declaration.exact_quote, html)
            self.assertIn("2026-09 ·", html)
            self.assertNotRegex(html, r"2026-09-01\s*·")
            self.assertIn(group.name, html)
            self.assertIn(declaration.capacity, html)
            self.assertIn("COLLECTIVE_AFFILIATION_DATED_V1", html)
            self.assertIn("Périmètre d’attribution", html)
            self.assertIn(declaration.speaker_person.display_name, html)
            _proof_step(
                "recette05_ok",
                f"record={dossier.stable_id} group={group.stable_id} speaker={declaration.speaker_person.stable_id}",
            )

    def test_recette05_date_precision_and_filters_http(self):
        coverage_2026 = {"period_start": "2026-01-01", "period_end": "2026-12-31"}
        user = self.make_user()
        holder = str(user.dv_profile.internal_uid)
        keys: dict = {}
        with self._loopback_patch(), self._live_settings(keys, holder):
            dossier_year, revision_year, _ = self._make_personal_date_chain(
                precision=Declaration.DatePrecision.YEAR,
                marker="YEAR",
                occurred_on=date(2026, 1, 1),
                coverage=coverage_2026,
            )
            keys = self._detached_approve(dossier_year, revision_year, user, keys)
            self._publish_cycle(dossier_year, revision_year, user, keys)
            html_year = self._comparer_html(dossier_year.stable_id)
            self.assertIn("RECETTE-05-YEAR", html_year)
            self.assertIn("2026 ·", html_year)
            self.assertNotRegex(html_year, r"2026-01-01\s*·")

            dossier_unknown, revision_unknown, _ = self._make_personal_date_chain(
                precision=Declaration.DatePrecision.UNKNOWN,
                marker="UNKNOWN",
                occurred_on=None,
                occurred_on_state=DataState.UNKNOWN,
                coverage=coverage_2026,
            )
            keys = self._detached_approve(dossier_unknown, revision_unknown, user, keys)
            self._publish_cycle(dossier_unknown, revision_unknown, user, keys)
            html_unknown = self._comparer_html(dossier_unknown.stable_id)
            self.assertIn("Date non renseignée", html_unknown)

            dossier_est, revision_est, _ = self._make_personal_date_chain(
                precision=Declaration.DatePrecision.ESTIMATED,
                marker="ESTIMATED",
                occurred_on=date(2026, 3, 10),
                coverage=coverage_2026,
            )
            keys = self._detached_approve(dossier_est, revision_est, user, keys)
            self._publish_cycle(dossier_est, revision_est, user, keys)
            html_est = self._comparer_html(dossier_est.stable_id)
            self.assertIn("Date estimée · 2026-03-10", html_est)

            dossier_int, revision_int, declaration_int = self._make_personal_date_chain(
                precision=Declaration.DatePrecision.INTERVAL,
                marker="INTERVAL",
                occurred_on=date(2026, 6, 1),
                interval_end=date(2026, 9, 30),
                publication_date=date(2026, 12, 1),
                coverage=coverage_2026,
            )
            keys = self._detached_approve(dossier_int, revision_int, user, keys)
            self._publish_cycle(dossier_int, revision_int, user, keys)
            html_int = self._comparer_html(dossier_int.stable_id)
            self.assertIn("2026-06-01 — 2026-09-30", html_int)
            self.assertNotIn("2026-12-01", html_int)

            dossier_inc, revision_inc, _ = self._make_personal_date_chain(
                precision=Declaration.DatePrecision.INTERVAL,
                marker="INTERVAL-INC",
                occurred_on=date(2026, 6, 1),
                interval_end=None,
                coverage=coverage_2026,
            )
            keys = self._detached_approve(dossier_inc, revision_inc, user, keys)
            self._publish_cycle(dossier_inc, revision_inc, user, keys)
            html_inc = self._comparer_html(dossier_inc.stable_id)
            self.assertIn("Intervalle incomplet", html_inc)
            self.assertNotRegex(html_inc, r"2026-06-01\s*—\s*2026")

            self.assertIn("2026-01-01 — 2026-12-31", html_year)

            _proof_step(
                "recette05_dates_ok",
                f"year={dossier_year.stable_id} interval={dossier_int.stable_id}",
            )

            compared = revision_year.public_payload["actors"]["compared_actor"]
            person_id = compared["person_id"]
            grid_hit = self._comparaisons_grid_html(
                {
                    "acteur_type": "person",
                    "acteur_id": person_id,
                }
            )
            hit_links = re.findall(r"dossier=([^\"&\s]+)", grid_hit)
            self.assertIn(dossier_year.stable_id, hit_links)

            grid_miss = self._comparaisons_grid_html(
                {
                    "acteur_type": "person",
                    "acteur_id": "SYN:ACTEUR-INEXISTANT",
                }
            )
            miss_links = re.findall(r"dossier=([^\"&\s]+)", grid_miss)
            self.assertNotIn(dossier_year.stable_id, miss_links)
            self.assertEqual(miss_links, [])

            grid_period = self._comparaisons_grid_html(
                {
                    "periode": "2026",
                    "acteur_type": "person",
                    "acteur_id": person_id,
                }
            )
            period_links = re.findall(r"dossier=([^\"&\s]+)", grid_period)
            self.assertIn(dossier_year.stable_id, period_links)

            grid_period_miss = self._comparaisons_grid_html(
                {
                    "periode": "2010",
                    "acteur_type": "person",
                    "acteur_id": person_id,
                }
            )
            period_miss_links = re.findall(r"dossier=([^\"&\s]+)", grid_period_miss)
            self.assertNotIn(dossier_year.stable_id, period_miss_links)

            _proof_step(
                "recette05_actor_filters_ok",
                f"hit={dossier_year.stable_id} person={person_id} negative=0_links",
            )
            _proof_step(
                "recette05_periode_filters_ok",
                f"periode=2026 hit={dossier_year.stable_id} periode=2010 miss",
            )

    def test_recette07_replay_same_event_idempotent_http(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        with self._loopback_patch(), self._live_settings(keys, holder):
            event = authorize_and_queue(revision, "test")
            self._post_review_wp(dossier, revision, event)
            quote = revision.public_payload["declaration_quote"]
            packet = packet_for_event(event)
            first = post_packet_loopback(packet)
            projection_after_first = self._quote_projection_count(dossier.stable_id, quote)
            self.assertGreaterEqual(projection_after_first, 1)
            second = post_packet_loopback(packet)
            self.assertEqual(first.get("_loopback_http_status"), 201)
            self.assertEqual(second.get("_loopback_http_status"), 200)
            projection_after_replay = self._quote_projection_count(dossier.stable_id, quote)
            self.assertEqual(projection_after_replay, projection_after_first)
            conf_once = self._confirmation_get(str(event.event_id))
            conf_twice = self._confirmation_get(str(event.event_id))
            self.assertEqual(conf_once["statement"], conf_twice["statement"])
            self.assertEqual(conf_once["statement"]["event_digest"], event.payload_digest)
            tampered = deepcopy(packet)
            tampered["payload"] = deepcopy(packet["payload"])
            tampered["payload"]["public"] = deepcopy(packet["payload"]["public"])
            tampered["payload"]["public"]["short_answer"] += " RECETTE-07-TAMPER"
            unsigned = {k: v for k, v in tampered.items() if k not in {"signature", "signature_algorithm"}}
            tampered["signature"] = sign_packet(unsigned)
            with self.assertRaises(WordPressUnavailable) as conflict:
                post_packet_loopback(tampered)
            tamper_err = str(conflict.exception)
            tamper_code = "422" if "loopback_http_422" in tamper_err else "409"
            self.assertIn(f"loopback_http_{tamper_code}", tamper_err, tamper_err)
            self.assertEqual(self._quote_projection_count(dossier.stable_id, quote), projection_after_replay)
            bundle_ok = build_review_bundle(revision, environment="test", destination="dv-site:test")
            bundle_alt = deepcopy(bundle_ok)
            bundle_alt["method_version"] = str(bundle_ok.get("method_version") or "DV-M0.1") + "-ALT-RECETTE07"
            review_key = _local_b_review_signing_key()
            keys_meta = json.loads(
                Path(os.environ.get("DV_LOCAL_B_TEST_KEYS_JSON", "")).read_text(encoding="utf-8")
            )
            review_date = event.payload["authorization"]["public_review"]["date"]
            stmt_alt = {
                "algorithm": "ed25519",
                "key_id": keys_meta["review_key_id"],
                "bundle_sha256": sha256_digest(bundle_alt),
                "record_id": dossier.stable_id,
                "record_revision": revision.revision,
                "decision": "approve",
                "reviewed_at": f"{review_date}T12:00:00+00:00",
            }
            proof_alt = {
                "statement": stmt_alt,
                "signature": base64.b64encode(review_key.sign(approval_bytes(bundle_alt, stmt_alt))).decode(),
            }
            status_rev, body_rev = self._post_rest_status(
                LOCAL_B_REVIEW_PATH,
                {"bundle": bundle_alt, "proof": proof_alt},
                idempotency_key=sha256_digest(bundle_alt),
            )
            self.assertIn(status_rev, {409, 422}, body_rev[:300])
            review_alt_code = status_rev
            out_of_order = deepcopy(packet)
            out_of_order["event_id"] = str(uuid.uuid4())
            out_of_order["sequence"] = max(1, int(packet["sequence"]) - 1)
            unsigned_oo = {k: v for k, v in out_of_order.items() if k not in {"signature", "signature_algorithm"}}
            out_of_order["signature"] = sign_packet(unsigned_oo)
            status_oo, body_oo = self._post_rest_status(
                LOCAL_B_EVENT_PATH,
                out_of_order,
                idempotency_key=out_of_order["event_id"],
            )
            self.assertIn(status_oo, {409, 422}, body_oo[:300])
            self.assertEqual(self._quote_projection_count(dossier.stable_id, quote), projection_after_replay)
            _proof_step(
                "recette07_idempotent",
                f"event={event.event_id} http=201,200 proj={projection_after_replay} "
                f"tamper={tamper_code} review_alt={review_alt_code} out_of_order={status_oo}",
            )

    def test_recette06_wp_refusals_http(self):
        user = self.make_user()
        holder = str(user.dv_profile.internal_uid)
        keys: dict = {}
        observed: list[str] = []
        with self._loopback_patch():
            declaration, _, scrutiny, _ = self.make_chain(person=True)
            declaration.position_scope = Declaration.PositionScope.PERSONAL
            declaration.save(update_fields=["position_scope", "updated_at"])
            dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
            quote = revision.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier, revision, user, keys)
            with self._live_settings(keys, holder):
                event = authorize_and_queue(revision, "test")
                packet = packet_for_event(event)
            status, body = self._post_rest_status(
                LOCAL_B_EVENT_PATH, packet, idempotency_key=str(event.event_id)
            )
            observed.append(f"no_review={status}")
            self.assertIn(status, {409, 422}, body)
            self._assert_unpublished(dossier.stable_id, quote)

            declaration2, _, scrutiny2, _ = self.make_chain(person=True)
            declaration2.position_scope = Declaration.PositionScope.PERSONAL
            declaration2.save(update_fields=["position_scope", "updated_at"])
            dossier2, revision2, _ = self._revision_with_comparison(declaration2, scrutiny2)
            quote2 = revision2.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier2, revision2, user, keys)
            with self._live_settings(keys, holder):
                event2 = authorize_and_queue(revision2, "test")
            bundle = build_review_bundle(revision2, environment="test", destination="dv-site:test")
            review_key = _local_b_review_signing_key()
            keys_meta = json.loads(Path(os.environ["DV_LOCAL_B_TEST_KEYS_JSON"]).read_text(encoding="utf-8"))
            review_date = event2.payload["authorization"]["public_review"]["date"]
            statement = {
                "algorithm": "ed25519",
                "key_id": keys_meta["review_key_id"],
                "bundle_sha256": sha256_digest(bundle),
                "record_id": dossier2.stable_id,
                "record_revision": revision2.revision,
                "decision": "approve",
                "reviewed_at": f"{review_date}T12:00:00+00:00",
            }
            proof = {
                "statement": statement,
                "signature": base64.b64encode(review_key.sign(approval_bytes(bundle, statement))).decode()[:-4] + "AAAA",
            }
            status_sig, body_sig = self._post_rest_status(
                LOCAL_B_REVIEW_PATH,
                {"bundle": bundle, "proof": proof},
                idempotency_key=sha256_digest(bundle),
            )
            observed.append(f"bad_signature={status_sig}")
            self.assertEqual(status_sig, 422, body_sig[:300])
            self._assert_unpublished(dossier2.stable_id, quote2)

            declaration3, _, scrutiny3, _ = self.make_chain(person=True)
            declaration3.position_scope = Declaration.PositionScope.PERSONAL
            declaration3.save(update_fields=["position_scope", "updated_at"])
            dossier3, revision3, _ = self._revision_with_comparison(declaration3, scrutiny3)
            quote3 = revision3.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier3, revision3, user, keys)
            with self._live_settings(keys, holder):
                event3 = authorize_and_queue(revision3, "test")
            bundle3 = build_review_bundle(revision3, environment="test", destination="dv-site:test")
            statement3 = {
                "algorithm": "ed25519",
                "key_id": keys_meta["review_key_id"],
                "bundle_sha256": sha256_digest(bundle3),
                "record_id": dossier3.stable_id,
                "record_revision": revision3.revision,
                "decision": "approve",
                "reviewed_at": f"{event3.payload['authorization']['public_review']['date']}T12:00:00+00:00",
            }
            proof3 = {
                "statement": statement3,
                "signature": base64.b64encode(review_key.sign(approval_bytes(bundle3, statement3))).decode(),
            }
            status_auth, body_auth = self._post_rest_status(
                LOCAL_B_REVIEW_PATH,
                {"bundle": bundle3, "proof": proof3},
                idempotency_key=sha256_digest(bundle3),
                auth=False,
            )
            observed.append(f"non_habilite={status_auth}")
            self.assertIn(status_auth, {401, 403}, body_auth[:300])
            self._assert_unpublished(dossier3.stable_id, quote3)

            declaration4, _, scrutiny4, _ = self.make_chain(person=True)
            declaration4.position_scope = Declaration.PositionScope.PERSONAL
            declaration4.save(update_fields=["position_scope", "updated_at"])
            dossier4, revision4, _ = self._revision_with_comparison(declaration4, scrutiny4)
            quote4 = revision4.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier4, revision4, user, keys)
            with self._live_settings(keys, holder):
                event4 = authorize_and_queue(revision4, "test")
                self._post_review_wp(dossier4, revision4, event4)
                packet4 = packet_for_event(event4)
            with self._live_settings(keys, holder):
                packet4["payload"] = deepcopy(packet4["payload"])
                packet4["payload"]["public"]["declaration_source_url"] = "https://127.0.0.1/private-only"
                packet4["payload_digest"] = sha256_digest(packet4["payload"])
                unsigned4 = {k: v for k, v in packet4.items() if k not in {"signature", "signature_algorithm"}}
                packet4["signature"] = sign_packet(unsigned4)
            status_priv, body_priv = self._post_rest_status(
                LOCAL_B_EVENT_PATH, packet4, idempotency_key=str(event4.event_id)
            )
            observed.append(f"private_url={status_priv}")
            self.assertEqual(status_priv, 422, body_priv[:300])
            self._assert_unpublished(dossier4.stable_id, quote4)

            declaration5, _, scrutiny5, _ = self.make_chain(person=True)
            declaration5.position_scope = Declaration.PositionScope.PERSONAL
            declaration5.save(update_fields=["position_scope", "updated_at"])
            dossier5, revision5, _ = self._revision_with_comparison(declaration5, scrutiny5)
            quote5 = revision5.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier5, revision5, user, keys)
            with self._live_settings(keys, holder):
                event5 = authorize_and_queue(revision5, "test")
                self._post_review_wp(dossier5, revision5, event5)
                packet5 = packet_for_event(event5)
            with self._live_settings(keys, holder):
                packet5["payload"] = deepcopy(packet5["payload"])
                packet5["payload"]["data_scope"] = "real"
                packet5["payload_digest"] = sha256_digest(packet5["payload"])
                unsigned5 = {k: v for k, v in packet5.items() if k not in {"signature", "signature_algorithm"}}
                packet5["signature"] = sign_packet(unsigned5)
            status_q, body_q = self._post_rest_status(
                LOCAL_B_EVENT_PATH, packet5, idempotency_key=str(event5.event_id)
            )
            observed.append(f"quarantine_scope={status_q}")
            self.assertEqual(status_q, 422, body_q[:300])
            self._assert_unpublished(dossier5.stable_id, quote5)

            declaration6, _, scrutiny6, _ = self.make_chain(person=True)
            declaration6.position_scope = Declaration.PositionScope.PERSONAL
            declaration6.save(update_fields=["position_scope", "updated_at"])
            dossier6, revision6, _ = self._revision_with_comparison(declaration6, scrutiny6)
            quote6 = revision6.public_payload["declaration_quote"]
            keys = self._detached_approve(dossier6, revision6, user, keys)
            with self._live_settings(keys, holder):
                event6 = authorize_and_queue(revision6, "test")
                self._post_review_wp(dossier6, revision6, event6)
                packet6 = packet_for_event(event6)
            packet6["destination"] = "dv-site:production"
            packet6["signature"] = "0" * 64
            status_dest, body_dest = self._post_rest_status(
                LOCAL_B_EVENT_PATH, packet6, idempotency_key=str(event6.event_id)
            )
            observed.append(f"test_to_production_sim={status_dest}")
            self.assertIn(status_dest, {403, 422}, body_dest[:300])
            self._assert_unpublished(dossier6.stable_id, quote6)

        _proof_step("recette06_refusals", " ".join(observed) + " " + php_invocation_proof())

    def test_recette10_transient_retry_bounded_http(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        user = self.make_user()
        keys = self._detached_approve(dossier, revision, user, {})
        holder = str(user.dv_profile.internal_uid)
        quote = revision.public_payload["declaration_quote"]
        env_extra = {
            "DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR": "1",
            "DV_WORDPRESS_ENDPOINT": LOCAL_B_EVENT_PATH,
            "DV_WORDPRESS_USERNAME": os.environ.get("DV_LOCAL_B_WP_USER", "loopback"),
            "DV_WORDPRESS_APP_PASSWORD": os.environ.get("DV_LOCAL_B_WP_PASSWORD", "loopback"),
        }
        env_extra["DV_WORDPRESS_LOOPBACK_CONNECT_PORTS"] = "10040,12789"
        with fault_server_session() as fault_port, self._loopback_patch(), self._live_settings(
            keys, holder
        ), override_settings(
            DV_PUBLICATION_SEND_AUTHORIZED=True,
            DV_RECEIVER_STATE_RECONCILED=True,
            DV_NETWORK_MODE="direct",
        ), patch.dict(os.environ, env_extra, clear=False):
            reset_loopback_fault_queue()
            event = authorize_and_queue(revision, "test")
            self._post_review_wp(dossier, revision, event)
            packet = packet_for_event(event)
            digest_before = event.payload_digest
            event_id = str(event.event_id)
            enqueue_loopback_faults("http429")
            with self.assertRaises(WordPressUnavailable) as transient:
                send_packet(packet)
            self.assertIn("429", str(transient.exception))
            inj_hits = [line for line in pop_request_log() if "/dv-loopback-fault/429" in line]
            self.assertTrue(inj_hits, "preuve HTTP 429 réelle via injecteur loopback")
            event.refresh_from_db()
            self.assertEqual(event.state, OutboxEvent.State.PENDING)
            self.assertEqual(str(event.event_id), event_id)
            self.assertEqual(event.payload_digest, digest_before)
            self._assert_unpublished(dossier.stable_id, quote)
            reset_loopback_fault_queue()
            send_packet(packet_for_event(event))
            event.refresh_from_db()
            self.assertEqual(event.state, OutboxEvent.State.DELIVERED)
            conf = self._confirmation_get(event_id)
            self.assertEqual(conf["statement"]["event_digest"], digest_before)
            self.assertGreaterEqual(self._quote_projection_count(dossier.stable_id, quote), 1)
            _proof_step(
                "recette10_retry",
                f"event={event_id} http429_port={fault_port} inj={inj_hits[0]} delivered digest={digest_before[:16]}",
            )
