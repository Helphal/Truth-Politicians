"""Chaîne HTTP Outil (runserver) → WordPress Local B — RECETTE-01/02/03/04."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from atelier.models import EditorialDossier, HumanReview, OutboxEvent
from atelier.services.racc001_loopback_http_client import MISSION_HTTP_ORIGIN, Racc001LoopbackHttpClient, _assert_mission_url
from atelier.services.loopback_fault_injector import fault_server_session, pop_request_log
from atelier.services.wordpress_loopback import enqueue_loopback_faults, reset_loopback_fault_queue
from atelier.services.publication import packet_for_event
from atelier.services.racc001_wp_loopback_delivery import (
    LOCAL_B_EVENT_PATH,
    accept_wp_confirmation,
    deliver_queued_event,
    fetch_comparer_html,
    fetch_wp_confirmation,
    post_review_to_wp,
)
from atelier.services.wordpress import WordPressUnavailable, send_packet
from atelier.tests.test_racc001_r5_local_loopback import (
    LOCAL_B_RECEIPT_KEY_ID,
    _local_b_site_confirmation_public_keys,
    _local_b_transport_keys,
)


class Command(BaseCommand):
    help = "Revue HTTP titulaire sur Outil, livraison loopback WP, confirmation et rendu comparer."

    def _wp_settings(self):
        return override_settings(
            DV_EVENT_KEY_ID="test-v2-event",
            DV_EVENT_KEYS=_local_b_transport_keys(),
            DV_RECEIPT_KEY_ID=LOCAL_B_RECEIPT_KEY_ID,
            DV_SITE_PUBLICATION_PUBLIC_KEYS=_local_b_site_confirmation_public_keys(),
            DV_PUBLICATION_SEND_AUTHORIZED=True,
            DV_RECEIVER_STATE_RECONCILED=True,
        )

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default=MISSION_HTTP_ORIGIN)
        parser.add_argument("--manifest", required=True)
        parser.add_argument(
            "--scenario",
            choices=("01", "02", "03", "04", "10", "all"),
            default="all",
        )

    def _holder_login(self, manifest: dict, base: str, label: str, lines: list[str]) -> Racc001LoopbackHttpClient:
        client = Racc001LoopbackHttpClient(base)
        status, authed = client.login_outcome(manifest["holder_username"], manifest["holder_password"])
        lines.append(f"{label}_login status={status} authed={authed}")
        if not authed:
            raise CommandError(f"{label}_login_failed")
        return client

    def _http_approve(
        self,
        client: Racc001LoopbackHttpClient,
        manifest: dict,
        case: dict,
        note: str,
        lines: list[str],
        label: str,
    ) -> HumanReview:
        dossier_id = case["dossier_id"]
        revision_no = case["revision"]
        dossier = EditorialDossier.objects.get(pk=dossier_id)
        revision = dossier.revisions.get(revision=revision_no)
        before_ids = set(HumanReview.objects.filter(revision=revision).values_list("pk", flat=True))
        csrf = client.csrf_from_page(f"/dossiers/{dossier_id}/")
        review_path = f"/dossiers/{dossier_id}/"
        status, body = client.request(
            "POST",
            f"/dossiers/{dossier_id}/action/",
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "review",
                "expected_revision": str(revision_no),
                "expected_digest": case["snapshot_digest"],
                "decision": "approve",
                "note": note,
            },
            referer_path=review_path,
        )
        lines.append(f"{label}_holder_post status={status}")
        if status not in {200, 302}:
            snippet = body.replace("\n", " ")[:240]
            raise CommandError(f"{label}_holder_approve_http_failed status={status} body={snippet}")
        review = (
            HumanReview.objects.filter(revision=revision, decision=HumanReview.Decision.APPROVE)
            .order_by("-created_at")
            .first()
        )
        if review is None or review.pk in before_ids:
            raise CommandError(f"{label}_holder_review_delta_missing")
        holder = get_user_model().objects.get(username=manifest["holder_username"])
        if review.reviewer_id != holder.pk:
            raise CommandError(f"{label}_holder_reviewer_mismatch")
        if review.reviewed_digest != case["snapshot_digest"]:
            raise CommandError(f"{label}_holder_digest_mismatch")
        if review.authority_internal_uid != manifest["holder_internal_uid"]:
            raise CommandError(f"{label}_holder_authority_mismatch")
        lines.append(
            f"{label}_holder_review id={review.pk} revision={revision_no} authority={review.authority_internal_uid}"
        )
        return review

    def _deliver(self, case: dict, label: str, lines: list[str]) -> None:
        dossier = EditorialDossier.objects.get(pk=case["dossier_id"])
        revision = dossier.revisions.get(revision=case["revision"])
        event = (
            OutboxEvent.objects.filter(dossier_id=case["dossier_id"])
            .order_by("-created_at", "-sequence")
            .first()
        )
        if event is None:
            raise CommandError(f"{label}_outbox_missing")
        with self._wp_settings():
            delivery = deliver_queued_event(dossier, revision, event)
        lines.append(
            f"{label}_wp_delivery review_http={delivery['review_http']} "
            f"event_http={delivery['event_http']} confirmation={delivery['confirmation_status']}"
        )

    def _run_recette_01(self, manifest: dict, base: str, lines: list[str]) -> None:
        case = manifest["recette_01"]
        if case.get("content_kind") != EditorialDossier.Kind.OFFICIAL_VOTE:
            raise CommandError("recette01_content_kind_invalid")
        client = self._holder_login(manifest, base, "recette01", lines)
        self._http_approve(client, manifest, case, "RECETTE-01 official vote HTTP", lines, "recette01")
        self._deliver(case, "recette01", lines)
        html_status, html = fetch_comparer_html(case["record_id"])
        lines.append(f"recette01_comparer status={html_status}")
        if html_status != 200:
            raise CommandError("recette01_comparer_not_200")
        if case["official_title"] and case["official_title"] not in html:
            raise CommandError("recette01_official_title_missing")
        if case["official_text_url"] not in html or case["official_vote_url"] not in html:
            raise CommandError("recette01_official_links_missing")
        lines.append("RECETTE_01_HTTP_CHAIN_OK")

    def _run_recette_02(self, manifest: dict, base: str, lines: list[str]) -> None:
        case = manifest["recette_02"]
        if case.get("content_kind") != EditorialDossier.Kind.VOTE_COMPARISON:
            raise CommandError("recette02_content_kind_invalid")
        client = self._holder_login(manifest, base, "recette02", lines)
        self._http_approve(client, manifest, case, "RECETTE-02 vote-vote HTTP", lines, "recette02")
        self._deliver(case, "recette02", lines)
        html_status, html = fetch_comparer_html(case["record_id"])
        lines.append(f"recette02_comparer status={html_status}")
        if html_status != 200:
            raise CommandError("recette02_comparer_not_200")
        if case["rule_id"] not in html:
            raise CommandError("recette02_rule_missing")
        if case["official_text_url"] not in html or case["official_vote_url"] not in html:
            raise CommandError("recette02_bases_missing")
        for key in ("calculation_numerator", "calculation_denominator", "calculation_excluded"):
            value = case.get(key)
            if value is not None and str(value) not in html:
                raise CommandError(f"recette02_{key}_missing")
        if case.get("left_person_name") and case["left_person_name"] not in html:
            raise CommandError("recette02_left_identity_missing")
        if case.get("right_person_name") and case["right_person_name"] not in html:
            raise CommandError("recette02_right_identity_missing")
        lines.append("RECETTE_02_HTTP_CHAIN_OK")

    def _run_recette_03(self, manifest: dict, base: str, lines: list[str]) -> None:
        case = manifest["recette_03"]
        client = self._holder_login(manifest, base, "recette03", lines)
        self._http_approve(client, manifest, case, "RECETTE-03 personal HTTP", lines, "recette03")
        if case["compared_actor_scope"] != "personal":
            raise CommandError("recette03_scope_not_personal")
        self._deliver(case, "recette03", lines)
        html_status, html = fetch_comparer_html(case["record_id"])
        lines.append(f"recette03_comparer status={html_status}")
        if html_status != 200 or case["declaration_quote"] not in html:
            raise CommandError("recette03_render_missing")
        lines.append("RECETTE_03_HTTP_CHAIN_OK")

    def _run_recette_04(self, manifest: dict, base: str, lines: list[str]) -> None:
        case = manifest["recette_04"]
        client = self._holder_login(manifest, base, "recette04", lines)
        self._http_approve(client, manifest, case, "RECETTE-04 collective HTTP", lines, "recette04")
        self._deliver(case, "recette04", lines)
        html_status, html = fetch_comparer_html(case["record_id"])
        lines.append(f"recette04_comparer status={html_status}")
        if html_status != 200:
            raise CommandError("recette04_comparer_not_200")
        if case["speaker_name"] not in html:
            raise CommandError("recette04_speaker_missing")
        if case["organization_name"] not in html:
            raise CommandError("recette04_organization_missing")
        if "Périmètre d’attribution" not in html and "Périmètre d'attribution" not in html:
            raise CommandError("recette04_attribution_scope_missing")
        if case["declaration_quote"] not in html:
            raise CommandError("recette04_quote_missing")
        if case.get("collective_distribution_state") != "available":
            raise CommandError("recette04_collective_state_not_available")
        if case.get("collective_rule_id") and case["collective_rule_id"] not in html:
            raise CommandError("recette04_collective_rule_missing")
        if case.get("collective_as_of") and case["collective_as_of"] not in html:
            raise CommandError("recette04_collective_as_of_missing")
        if int(case.get("included_count") or 0) < 1:
            raise CommandError("recette04_included_members_missing")
        if int(case.get("excluded_count") or 0) > 0 and "exclusion" not in html.lower():
            raise CommandError("recette04_exclusions_not_visible")
        if case["speaker_name"] == case["organization_name"]:
            raise CommandError("recette04_speaker_not_distinct_from_organization")
        lines.append("RECETTE_04_HTTP_CHAIN_OK")

    def _run_recette_10(self, manifest: dict, base: str, lines: list[str]) -> None:
        """Timeout socket réel (injecteur) puis reprise : send_packet strict, même event_id/digest."""
        case = manifest["recette_03"]
        client = self._holder_login(manifest, base, "recette10", lines)
        self._http_approve(client, manifest, case, "RECETTE-10 timeout HTTP chain", lines, "recette10")
        dossier = EditorialDossier.objects.get(pk=case["dossier_id"])
        revision = dossier.revisions.get(revision=case["revision"])
        event = (
            OutboxEvent.objects.filter(dossier_id=case["dossier_id"])
            .order_by("-created_at", "-sequence")
            .first()
        )
        if event is None:
            raise CommandError("recette10_outbox_missing")
        event_id = str(event.event_id)
        digest_before = event.payload_digest
        wp_user = os.environ.get("DV_LOCAL_B_WP_USER", "loopback")
        wp_pass = os.environ.get("DV_LOCAL_B_WP_PASSWORD", "loopback")
        env_extra = {
            "DV_WORDPRESS_LOOPBACK_FAULT_INJECTOR": "1",
            "DV_WORDPRESS_ENDPOINT": LOCAL_B_EVENT_PATH,
            "DV_WORDPRESS_USERNAME": wp_user,
            "DV_WORDPRESS_APP_PASSWORD": wp_pass,
            "DV_WORDPRESS_LOOPBACK_CONNECT_PORTS": "10040,12789",
            "DV_WORDPRESS_LOOPBACK_FAULT_TIMEOUT_DELAY": "8",
        }
        with self._wp_settings(), override_settings(DV_NETWORK_MODE="direct"):
            review_resp = post_review_to_wp(dossier, revision, event)
            lines.append(f"recette10_review_http={review_resp.get('_loopback_http_status')}")
            packet = packet_for_event(event)
            with fault_server_session() as fault_port, patch.dict(os.environ, env_extra, clear=False):
                reset_loopback_fault_queue()
                enqueue_loopback_faults("http_timeout")
                try:
                    send_packet(packet, timeout=3.0)
                except WordPressUnavailable as exc:
                    lines.append(f"recette10_timeout_attempt err={exc}")
                else:
                    raise CommandError("recette10_timeout_expected_success")
                inj_hits = [ln for ln in pop_request_log() if "/dv-loopback-fault/timeout" in ln]
                if not inj_hits:
                    raise CommandError("recette10_injector_miss")
                lines.append(f"recette10_injector_port={fault_port} hit={inj_hits[0]}")
                event.refresh_from_db()
                if event.state != OutboxEvent.State.PENDING:
                    raise CommandError(f"recette10_not_pending state={event.state}")
                if str(event.event_id) != event_id:
                    raise CommandError("recette10_event_id_changed")
                if event.payload_digest != digest_before:
                    raise CommandError("recette10_digest_changed")
                reset_loopback_fault_queue()
                send_packet(packet_for_event(event))
                event.refresh_from_db()
                if event.state != OutboxEvent.State.DELIVERED:
                    raise CommandError(f"recette10_not_delivered state={event.state}")
                conf = fetch_wp_confirmation(event)
                stmt = conf.get("statement") or {}
                if stmt.get("event_digest") != digest_before:
                    raise CommandError("recette10_confirmation_digest_mismatch")
                accept_wp_confirmation(event, conf)
                lines.append(f"recette10_confirmation status={stmt.get('status')}")
        lines.append("RECETTE_10_HTTP_CHAIN_TIMEOUT_OK")

    def handle(self, *args, **options):
        if os.environ.get("DV_WORDPRESS_LOOPBACK_TEST", "").strip() != "1":
            raise CommandError("DV_WORDPRESS_LOOPBACK_TEST_required")
        manifest = json.loads(Path(options["manifest"]).read_text(encoding="utf-8"))
        base = options["base_url"]
        _assert_mission_url(base)
        if str(manifest.get("holder_internal_uid")) != str(
            getattr(settings, "DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID", "")
        ):
            raise CommandError("holder_uid_env_mismatch")

        lines: list[str] = [f"run_id={manifest.get('run_id', '')}", f"base_url={base}"]
        scenario = options["scenario"]
        runners = {
            "01": self._run_recette_01,
            "02": self._run_recette_02,
            "03": self._run_recette_03,
            "04": self._run_recette_04,
            "10": self._run_recette_10,
        }
        if scenario == "all":
            for key in ("01", "02", "03", "04"):
                runners[key](manifest, base, lines)
        else:
            runners[scenario](manifest, base, lines)

        for line in lines:
            if "PARTIAL" in line or line.endswith("_FAILED"):
                raise CommandError("http_chain_incomplete_or_partial")
        required_ok = (
            "RECETTE_01_HTTP_CHAIN_OK",
            "RECETTE_02_HTTP_CHAIN_OK",
            "RECETTE_03_HTTP_CHAIN_OK",
            "RECETTE_04_HTTP_CHAIN_OK",
        )
        if scenario == "all":
            for marker in required_ok:
                if marker not in lines:
                    raise CommandError(f"missing_marker:{marker}")

        self.stdout.write("\n".join(lines))
        self.stdout.write("HTTP_CHAIN_WP_OK")
