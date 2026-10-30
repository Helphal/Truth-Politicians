"""Tests fictifs et hors réseau. Aucune revue humaine réelle n'est créée."""
import copy
import json
from pathlib import Path
import unittest

from reference.publication_gate import (
    SCHEMA, POLICY, REQUIRED_CHECKS, ControlAttestation, TrustedReviewEvent,
    PublicationCursor, digest, evaluate, pair_counts, validate_candidate, InvalidCandidate,
)

# Tous les personnages/citations de ce module sont fictifs.
TEXT_URL = "https://example.org/fixtures/text-not-fetched"
VOTE_URL = "https://example.org/fixtures/vote-not-fetched"
SPEECH_URL = "https://example.org/fixtures/speech-not-fetched"
REVIEWER = "ACTEUR_FICTIF_TEST_UNIQUEMENT"


def candidate(kind="speech_vote"):
    c = {"schema_version": SCHEMA, "record_id": "FIXTURE-NON-PUBLIABLE-001",
         "record_revision": 1, "content_kind": kind, "policy_version": POLICY,
         "method_version": "M0.1-test", "data_scope": "synthetic", "source_versions": [
             {"source_id": "S-TEXT", "version": "fixture-v1", "public_url": TEXT_URL},
             {"source_id": "S-VOTE", "version": "fixture-v1", "public_url": VOTE_URL},
             {"source_id": "S-SPEECH", "version": "fixture-v1", "public_url": SPEECH_URL}],
         "public": {}}
    if kind == "official_vote":
        c["public"] = {"institution": "INSTITUTION FICTIVE DE TEST", "official_title": "Texte fictif de test",
                       "scrutin_id": "TEST-SCRUTIN-1", "occurred_on": "2024-01-30", "stage": "étape fictive",
                       "official_text_url": TEXT_URL, "official_vote_url": VOTE_URL,
                       "text_reference_role": "submitted", "nominative_status": "available",
                       "counts": {"for": 2, "against": 1, "abstention": 0}}
    elif kind == "vote_comparison":
        c["public"] = {"rule_id": "PAIR_EQUAL_KNOWN_V1", "scope": "Univers fictif de test",
                       "observations": [{"scrutin_id": "TEST-SCRUTIN-1", "left": "for", "right": "for",
                                         "official_text_url": TEXT_URL, "official_vote_url": VOTE_URL}]}
    else:
        c["public"] = {"title": "DOSSIER FICTIF DE TEST", "short_answer": "Réponse fictive",
                       "main_caveat": "Test, pas une conclusion politique.", "reasoning": "Justification fictive",
                       "result": "convergence", "declaration_quote": "Citation inventée pour un test logiciel.",
                       "declaration_source_url": SPEECH_URL,
                       "official_text_url": TEXT_URL, "official_vote_url": VOTE_URL}
    return c


def controls(c, missing=(), failed=()):
    return ControlAttestation(digest(c), REQUIRED_CHECKS[c["content_kind"]] - set(missing), tuple(failed))


def current(c):
    return {s["source_id"]: s["version"] for s in c["source_versions"]}


def review(c, **overrides):
    args = {"record_id": c["record_id"], "revision": c["record_revision"], "snapshot_digest": digest(c),
            "reviewer_id": REVIEWER, "decision": "approve", "environment": "test", "revoked": False}
    args.update(overrides)
    return TrustedReviewEvent(**args)


def checked(c, **overrides):
    args = {"candidate": c, "controls": controls(c), "current_source_versions": current(c)}
    args.update(overrides)
    return evaluate(**args)


class PublicationGateTests(unittest.TestCase):
    def test_01_official_vote_needs_no_human_review(self):
        e = checked(candidate("official_vote"))
        self.assertTrue(e.allowed); self.assertEqual(e.route, "automatic_checked")

    def test_02_speech_without_review_is_blocked(self):
        self.assertFalse(checked(candidate()).allowed)

    def test_03_test_review_of_exact_snapshot_is_eligible_not_published(self):
        c = candidate()
        e = checked(c, trusted_reviews=[review(c)], registered_reviewers=frozenset({REVIEWER}))
        self.assertTrue(e.allowed); self.assertEqual(e.reason, "eligible_not_published")

    def test_04_json_cannot_inject_human_approval(self):
        c = candidate(); c["approved_by_human"] = True
        self.assertFalse(checked(c).allowed)

    def test_05_json_cannot_inject_internal_reviewer(self):
        c = candidate(); c["public"]["reviewer_email"] = "sentinelle@example.invalid"
        self.assertFalse(checked(c).allowed)

    def test_06_revision_change_invalidates_review(self):
        c = candidate(); r = review(c); c["record_revision"] = 2
        self.assertFalse(checked(c, trusted_reviews=[r], registered_reviewers=frozenset({REVIEWER})).allowed)

    def test_07_caveat_change_invalidates_review(self):
        c = candidate(); r = review(c); c["public"]["main_caveat"] = "Autre réserve fictive"
        self.assertFalse(checked(c, trusted_reviews=[r], registered_reviewers=frozenset({REVIEWER})).allowed)

    def test_08_changed_source_state_blocks(self):
        c = candidate("official_vote"); state = current(c); state["S-VOTE"] = "fixture-v2"
        self.assertEqual(checked(c, current_source_versions=state).reason, "source_changed_or_unknown")

    def test_09_missing_source_state_blocks(self):
        c = candidate("official_vote"); state = current(c); del state["S-TEXT"]
        self.assertFalse(checked(c, current_source_versions=state).allowed)

    def test_10_stale_controls_block_even_facts(self):
        c = candidate("official_vote"); ctl = controls(c); c["public"]["counts"]["for"] = 3
        self.assertFalse(checked(c, controls=ctl).allowed)

    def test_11_failed_control_blocks(self):
        c = candidate("official_vote")
        self.assertFalse(checked(c, controls=controls(c, failed=["counts"])).allowed)

    def test_12_missing_required_control_blocks(self):
        c = candidate("official_vote")
        self.assertFalse(checked(c, controls=controls(c, missing=["reuse"])).allowed)

    def test_13_old_schema_is_not_silently_upgraded(self):
        c = candidate("official_vote"); c["schema_version"] = "0.2"
        self.assertFalse(checked(c).allowed)

    def test_14_new_policy_requires_explicit_implementation(self):
        c = candidate("official_vote"); c["policy_version"] = "inconnue"
        self.assertFalse(checked(c).allowed)

    def test_15_test_review_does_not_authorize_production(self):
        c = candidate()
        # Simulation locale du marquage réel, sans utiliser ni exporter un fait réel.
        c["data_scope"] = "real"
        result = checked(c, trusted_reviews=[review(c)], environment="production",
                         registered_reviewers=frozenset({REVIEWER}))
        self.assertEqual(result.reason, "review_missing_or_stale")

    def test_51_synthetic_facts_are_blocked_in_production(self):
        self.assertEqual(checked(candidate("official_vote"), environment="production").reason,
                         "synthetic_content_in_production")

    def test_16_unregistered_reviewer_blocks(self):
        c = candidate(); self.assertFalse(checked(c, trusted_reviews=[review(c)]).allowed)

    def test_17_latest_rejection_overrides_earlier_approval(self):
        c = candidate()
        self.assertFalse(checked(c, trusted_reviews=[review(c), review(c, decision="reject")],
                                 registered_reviewers=frozenset({REVIEWER})).allowed)

    def test_18_revoked_review_blocks(self):
        c = candidate()
        self.assertFalse(checked(c, trusted_reviews=[review(c, revoked=True)],
                                 registered_reviewers=frozenset({REVIEWER})).allowed)

    def test_19_suspension_blocks_even_checked_facts(self):
        self.assertEqual(checked(candidate("official_vote"), suspended=True).reason, "suspended")

    def test_20_speech_fields_cannot_be_retyped_as_official_vote(self):
        c = candidate(); c["content_kind"] = "official_vote"
        self.assertFalse(checked(c).allowed)

    def test_21_server_kind_registry_prevents_retyping(self):
        self.assertFalse(checked(candidate("official_vote"), established_kind="speech_vote").allowed)

    def test_22_ip_literal_in_public_link_is_blocked(self):
        c = candidate("official_vote"); c["public"]["official_text_url"] = "https://127.0.0.1/a"
        self.assertFalse(checked(c).allowed)

    def test_23_private_hostname_is_blocked(self):
        c = candidate("official_vote"); c["source_versions"][0]["public_url"] = "https://prive.local/a"
        self.assertFalse(checked(c).allowed)

    def test_24_userinfo_is_blocked(self):
        c = candidate("official_vote"); c["source_versions"][0]["public_url"] = "https://secret:secret@exemple.fr/a"
        self.assertFalse(checked(c).allowed)

    def test_25_non_https_is_blocked(self):
        c = candidate("official_vote"); c["source_versions"][0]["public_url"] = "http://exemple.fr/a"
        self.assertFalse(checked(c).allowed)

    def test_26_resulting_text_only_is_not_voted_text(self):
        c = candidate("official_vote"); c["public"]["text_reference_role"] = "result_only"
        self.assertEqual(checked(c).reason, "voted_text_unresolved")

    def test_27_missing_full_text_does_not_block(self):
        # Aucun champ de texte intégral n'est exigé par ce brouillon.
        self.assertTrue(checked(candidate("official_vote")).allowed)

    def test_28_null_count_remains_null(self):
        c = candidate("official_vote"); c["public"]["counts"]["abstention"] = None
        validate_candidate(c)
        self.assertIsNone(c["public"]["counts"]["abstention"])

    def test_29_bool_is_not_a_vote_count(self):
        c = candidate("official_vote"); c["public"]["counts"]["for"] = True
        self.assertFalse(checked(c).allowed)

    def test_30_agreement_excludes_unknown_and_non_voting(self):
        counts = pair_counts([{"left":"for","right":"for"}, {"left":None,"right":"against"},
                              {"left":"non_voting","right":"non_voting"},
                              {"left":"abstention","right":"abstention"},
                              {"left":"against","right":"for"}])
        self.assertEqual(counts["same_position"], 2); self.assertEqual(counts["denominator"], 3)
        self.assertEqual(counts["excluded"], 2)

    def test_31_zero_denominator_is_not_zero_percent(self):
        self.assertIsNone(pair_counts([{"left":None,"right":None}])["agreement_fraction"])

    def test_32_fact_comparison_is_eligible_without_human(self):
        self.assertTrue(checked(candidate("vote_comparison")).allowed)

    def test_33_unknown_vote_category_is_not_normalized_silently(self):
        c = candidate("vote_comparison"); c["public"]["observations"][0]["left"] = "absent_physiquement"
        self.assertFalse(checked(c).allowed)

    def test_34_duplicate_scrutin_is_rejected(self):
        c = candidate("vote_comparison"); c["public"]["observations"] *= 2
        self.assertFalse(checked(c).allowed)

    def test_35_unregistered_link_is_rejected(self):
        c = candidate("official_vote"); c["public"]["official_text_url"] = "https://www.senat.fr/un-autre-texte"
        self.assertFalse(checked(c).allowed)

    def test_36_empty_caveat_is_rejected(self):
        c = candidate(); c["public"]["main_caveat"] = " "
        self.assertFalse(checked(c).allowed)

    def test_37_liot_original_is_not_authorized(self):
        p = Path(__file__).resolve().parents[1] / "fixtures/DV_CAS_AN16_3289_LIOT.original.json"
        if not p.is_file():
            self.skipTest("Pièce documentaire privée non distribuée ; contrôle historique non exécuté")
        c = json.loads(p.read_text())
        self.assertFalse(c["controls"]["human_review"]["performed"])
        self.assertEqual(c["publication"]["authorization"], "non_accordee")
        self.assertFalse(evaluate(c, None, {}).allowed)

    def test_38_malformed_input_does_not_return_success(self):
        self.assertFalse(evaluate({}, None, {}).allowed)

    def test_39_method_change_invalidates_review(self):
        c = candidate(); r = review(c); c["method_version"] = "M0.2-test"
        self.assertFalse(checked(c, trusted_reviews=[r], registered_reviewers=frozenset({REVIEWER})).allowed)


class ReceiverSimulationTests(unittest.TestCase):
    def setUp(self):
        self.cursor = PublicationCursor("TEST-ID")
        self.cursor.apply("TEST-ID", 1, 1, "empreinte-fixture-1", "upsert", True)

    def test_40_exact_replay_is_idempotent(self):
        self.assertEqual(self.cursor.apply("TEST-ID",1,1,"empreinte-fixture-1","upsert",True),"duplicate")

    def test_41_same_sequence_different_event_is_rejected(self):
        self.assertEqual(self.cursor.apply("TEST-ID",1,1,"autre","upsert",True),"sequence_conflict")

    def test_42_same_revision_different_content_is_rejected(self):
        self.assertEqual(self.cursor.apply("TEST-ID",1,2,"autre","upsert",True),"revision_conflict")

    def test_43_stale_revision_is_rejected(self):
        self.cursor.apply("TEST-ID",2,2,"empreinte-fixture-2","upsert",True)
        self.assertEqual(self.cursor.apply("TEST-ID",1,3,"empreinte-fixture-1","upsert",True),"stale_revision")

    def test_44_suspension_prevents_old_replay(self):
        self.cursor.apply("TEST-ID",1,2,"empreinte-fixture-1","suspend",True)
        self.assertEqual(self.cursor.apply("TEST-ID",1,1,"empreinte-fixture-1","upsert",True),"stale_event")
        self.assertTrue(self.cursor.suspended)

    def test_45_higher_sequence_cannot_clear_suspension(self):
        self.cursor.apply("TEST-ID",1,2,"empreinte-fixture-1","suspend",True)
        self.assertEqual(self.cursor.apply("TEST-ID",2,3,"empreinte-fixture-2","upsert",True),"suspension_latched")

    def test_46_unauthorized_event_is_refused(self):
        self.assertEqual(self.cursor.apply("TEST-ID",2,2,"empreinte-fixture-2","upsert",False),"unauthorized")

    def test_47_other_object_is_refused(self):
        self.assertEqual(self.cursor.apply("OTHER",2,2,"empreinte-fixture-2","upsert",True),"wrong_object")

    def test_48_authorized_resume_requires_separate_server_decision(self):
        self.cursor.apply("TEST-ID",1,2,"empreinte-fixture-1","suspend",True)
        result = self.cursor.apply("TEST-ID",2,3,"empreinte-fixture-2","upsert",True,resume_authorized=True)
        self.assertEqual(result,"applied_in_memory_only"); self.assertFalse(self.cursor.suspended)

    def test_49_reordered_suspend_is_not_silently_ignored(self):
        self.cursor.apply("TEST-ID",2,2,"empreinte-fixture-2","upsert",True)
        self.assertEqual(self.cursor.apply("TEST-ID",1,3,"empreinte-fixture-1","suspend",True),"applied_in_memory_only")
        self.assertTrue(self.cursor.suspended); self.assertEqual(self.cursor.revision,2)

    def test_50_unknown_operation_is_rejected(self):
        self.assertEqual(self.cursor.apply("TEST-ID",2,2,"empreinte-fixture-2","force_publish",True),"invalid_event")

if __name__ == "__main__":
    unittest.main()
