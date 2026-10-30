from copy import deepcopy
from django.core.exceptions import ValidationError
from django.test import override_settings
from atelier.models import Person, VoteObservation, CatalogueTransfer
from atelier.services.catalogue_transfer import build_catalogue_transfer, receive_catalogue_page, missing_pages
from atelier.services.canonical import sha256_digest
from atelier.tests.support import AtelierTestCase
import review_followup as q


@override_settings(DV_ENV="test", DV_EVENT_KEY_ID="test-v1", DV_REVIEW_ATTESTATION_KEY="synthetic-pagination-only")
class CatalogueTransferTests(AtelierTestCase):
    make_catalogue_revision = q.FollowupTests.make_catalogue_revision

    def transfer(self):
        _, _, doc, scrutiny = self.make_catalogue_revision()
        for i in range(575):
            person = Person.objects.create(stable_id=f"SYN:P:{i:04}", display_name=f"Fictif {i}")
            VoteObservation.objects.create(scrutiny=scrutiny, person=person, source_document=doc,
                source_category="unknown", normalized_category="unknown", mapping_rule="SYNTHETIC_MAPPING")
        return build_catalogue_transfer([scrutiny.stable_id])

    def test_one_scrutiny_577_observations_reverse_order_restart_and_duplicate(self):
        manifest, pages = self.transfer()
        self.assertTrue(all(len(p["objects"]) <= 250 for p in pages))
        first = receive_catalogue_page(manifest, pages[-1])
        transfer = CatalogueTransfer.objects.get(manifest_digest=first["transfer_id"])
        self.assertIsNone(transfer.assembled_catalogue)
        self.assertTrue(missing_pages(transfer))
        self.assertEqual(receive_catalogue_page(manifest, pages[-1]), first)
        for page in reversed(pages[:-1]):
            result = receive_catalogue_page(manifest, page)
        transfer.refresh_from_db()
        self.assertTrue(result["complete_locally"])
        self.assertFalse(result["site_activated"])
        self.assertEqual(len(transfer.assembled_catalogue["vote_observations"]), 577)
        self.assertEqual(sha256_digest(transfer.assembled_catalogue), manifest["snapshot_sha256"])
        self.assertEqual(transfer.pages.count(), len(pages))

    def test_altered_page_and_mixed_snapshot_rejected(self):
        manifest, pages = self.transfer()
        changed = deepcopy(pages[0])
        changed["objects"][0]["record"]["display_name"] = "Altéré"
        with self.assertRaisesMessage(ValidationError, "catalogue_page_hash_mismatch"):
            receive_catalogue_page(manifest, changed)
        changed = deepcopy(pages[0])
        changed["snapshot_sha256"] = "0" * 64
        with self.assertRaisesMessage(ValidationError, "catalogue_page_wrong_snapshot"):
            receive_catalogue_page(manifest, changed)
        with override_settings(DV_ENV="production"):
            with self.assertRaisesMessage(ValidationError, "catalogue_receiver_local_only"):
                receive_catalogue_page(manifest, pages[0])

    def test_repeat_builder_same_materialized_version_same_pages(self):
        manifest, pages = self.transfer()
        again, again_pages = build_catalogue_transfer(manifest["scope_scrutiny_ids"])
        self.assertEqual(manifest, again)
        self.assertEqual(pages, again_pages)
