"""EXPLAIN — retrait ciblé, seed inactif, réautorisation versionnée (CR04/R5)."""

from django.test import TestCase

from atelier.models import ExplainEntry
from atelier.services.explain import (
    public_projection,
    reauthorize_explain_entry,
    seed_explain_entries,
)


class ExplainWithdrawScopeTests(TestCase):
    def setUp(self):
        seed_explain_entries()

    def test_withdraw_export_only_marks_inactive_entries(self):
        sample = ExplainEntry.objects.filter(stable_id="explain.presidence").order_by("-version").first()
        self.assertIsNotNone(sample)
        sample.active = False
        sample.save(update_fields=["active", "updated_at"])
        payload = public_projection(authorization_state="withdrawn")
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["stable_id"], "explain.presidence")

    def test_inactive_seed_change_stays_inactive(self):
        entry = ExplainEntry.objects.filter(stable_id="explain.presidence").order_by("-version").first()
        entry.active = False
        entry.save(update_fields=["active", "updated_at"])
        before = entry.version
        entry.title = entry.title + " (contrôle seed)"
        entry.save(update_fields=["title", "updated_at"])
        seed_explain_entries()
        latest = ExplainEntry.objects.filter(stable_id="explain.presidence").order_by("-version").first()
        self.assertGreater(latest.version, before)
        self.assertFalse(latest.active)

    def test_reauthorize_bumps_version_and_active(self):
        entry = ExplainEntry.objects.filter(stable_id="explain.presidence").order_by("-version").first()
        entry.active = False
        entry.save(update_fields=["active", "updated_at"])
        before = entry.version
        restored = reauthorize_explain_entry("explain.presidence")
        self.assertTrue(restored.active)
        self.assertEqual(restored.version, before + 1)
        authorized = public_projection(authorization_state="authorized")
        ids = {item["stable_id"] for item in authorized["entries"]}
        self.assertIn("explain.presidence", ids)
