"""Recette réelle locale sur copie restaurée uniquement ; aucune revue humaine."""
import json
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from atelier.models import EditorialDossier, HumanReview, OutboxEvent, ReceiverCursor
from atelier.services.canonical import sha256_digest
from atelier.services.publication import evaluate_revision, authorize_and_queue, packet_for_event, reissue_expired_event
from atelier.services.receiver import apply_packet
from atelier.services.exporter import export_outbox
from atelier.services.inquiry import prepare_inquiry


class Command(BaseCommand):
    help = "Recette de contre-revue sur une copie restaurée isolée, réseau désactivé."

    def add_arguments(self, parser):
        parser.add_argument("--batch", default="outbox-draft3-test")

    def handle(self, *args, **options):
        db = Path(settings.DATABASES["default"]["NAME"]).resolve()
        original = (settings.BASE_DIR / "runtime/atelier.sqlite3").resolve()
        if settings.DV_ENV != "test" or settings.DV_NETWORK_MODE != "disabled" or db == original or not (settings.RUNTIME_DIR / ".dv-restored-unreconciled").exists():
            raise CommandError("isolated_restored_test_database_required")
        if HumanReview.objects.exists():
            raise CommandError("sample_must_have_zero_human_reviews")
        rows, packets = [], []
        for dossier in EditorialDossier.objects.order_by("stable_id"):
            revision = dossier.revisions.get(revision=dossier.current_revision)
            eligibility = evaluate_revision(revision, "test")
            row = {"record_id": dossier.stable_id, "revision": revision.revision, "kind": dossier.content_kind,
                "eligible": eligibility.allowed, "reason": eligibility.reason,
                "catalogue_collections": sorted(revision.public_payload.get("catalogue", {})),
                "acts": len(revision.public_payload.get("acts", [])),
                "source_versions": len(revision.source_snapshot), "snapshot_digest": revision.snapshot_digest}
            if dossier.content_kind != "speech_vote" and eligibility.allowed:
                event = authorize_and_queue(revision, "test")
                try:
                    packet = packet_for_event(event)
                except ValidationError as exc:
                    if exc.messages[0] != "event_expired_reissue_required":
                        raise
                    packet = packet_for_event(reissue_expired_event(event))
                row["receiver_first"] = apply_packet(packet)
                row["receiver_replay"] = apply_packet(packet)
                packets.append(packet)
            rows.append(row)
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", options["batch"]):
            raise CommandError("batch_name_invalid")
        batch = export_outbox(settings.RUNTIME_DIR / options["batch"], environment="test")
        inquiries = [prepare_inquiry(query=q) for q in ("hydrocarbures outre-mer", "liberté choisir femmes", "océanographie arctique")]
        result = {"scope": "isolated_real_sample_local_only", "content_schema": settings.DV_PUBLICATION_SCHEMA,
            "event_schema": settings.DV_EVENT_SCHEMA, "rows": rows, "export_batch": batch,
            "inquiries": inquiries, "human_reviews": HumanReview.objects.count(),
            "network_calls": 0, "wordpress_integration_verified": False, "private_service_verified": False,
            "public_release_authorized": False}
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
