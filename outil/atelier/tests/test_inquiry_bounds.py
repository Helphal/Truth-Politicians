from django.test import override_settings
from atelier.tests.support import AtelierTestCase
import atelier.tests.test_inquiry as previous
from atelier.models import Declaration
from atelier.services.inquiry import prepare_inquiry


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class InquiryBoundsTests(AtelierTestCase):
    make_novel_declaration = previous.NovelInquiryTests.make_novel_declaration

    def test_unsearched_tail_cannot_be_evidence_missing_and_can_resume(self):
        declaration, _ = self.make_novel_declaration()
        Declaration.objects.create(stable_id="AAA:UNRELATED", exact_quote="Texte autre",
            speaker_label="Fictif", capacity="Fictif", source_passage=declaration.source_passage)
        first = prepare_inquiry(query="bibliothèques rurales", scan_limit=1)
        self.assertEqual(first["state"], "search_incomplete")
        self.assertEqual(first["scan"]["declarations_remaining"], 1)
        second = prepare_inquiry(query="bibliothèques rurales", scan_limit=1,
            declaration_offset=first["scan"]["next_declaration_offset"])
        self.assertTrue(second["candidates"])
        self.assertFalse(second["scan"]["complete_official_corpus"])

    def test_post_vote_exclusion_retains_source_and_no_political_inference(self):
        self.make_novel_declaration()
        result = prepare_inquiry(query="bibliothèques rurales")
        exclusion = next(e for e in result["exclusions"] if e["reason"] == "after_vote_not_evidence_of_prior_promise")
        self.assertTrue(exclusion["source_version"]["sha256"])
        self.assertEqual(result["candidates"][0]["proposal"]["result"], "inconclusive")
