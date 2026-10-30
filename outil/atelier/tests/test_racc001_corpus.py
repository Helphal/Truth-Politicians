"""Corpus COMMUN R2/R3 + chaîne producteur Outil → validateur PHP."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from jsonschema import Draft202012Validator, FormatChecker

from atelier.models import (
    Comparison,
    ComparisonAct,
    DataState,
    Declaration,
    EditorialDossier,
    EvidencePassage,
    Organization,
    Person,
)
from atelier.services.catalogue import prepare_public_snapshot
from atelier.services.contracts import publication_schema_for, validate_contract_envelope
from atelier.services.publication import authorize_and_queue
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.services.thematic_integrity import qualified_declaration_payload
from atelier.tests.racc001_php import (
    RACC001_PRESS_PUBLIC_URL,
    SITE_RACC001_EVENT_SCHEMA_BILATERAL,
    php_argv,
    site_test_script,
)
from atelier.tests.support import AtelierTestCase
from atelier.tests.test_thematic_comparisons import ThematicComparisonTests as ThematicComparisonHarness


@override_settings(DV_ENV="test", DV_NETWORK_MODE="disabled")
class Racc001CorpusTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self._thematic = ThematicComparisonHarness()
        self._thematic.setUp()

    def make_chain(self, **kwargs):
        return self._thematic.make_chain(**kwargs)

    def _revision_with_comparison(
        self,
        declaration,
        scrutiny,
        *,
        data_scope: str = "synthetic",
        coverage: dict | None = None,
        stable_id: str | None = None,
    ):
        comparison = Comparison.objects.create(
            stable_id=self.unique("CMP"),
            declaration=declaration,
            result="inconclusive",
            criteria={},
            counterevidence=[],
            scope="Corpus RACC-001",
            rationale="Cas de test documentaire borné.",
        )
        ComparisonAct.objects.create(
            comparison=comparison,
            scrutiny=scrutiny,
            included=True,
            reason="scrutin de référence",
            chronological_relation="before_vote",
        )
        dossier = EditorialDossier.objects.create(
            stable_id=stable_id or self.unique("EDITORIAL"),
            content_kind=EditorialDossier.Kind.SPEECH_VOTE,
            established_kind=EditorialDossier.Kind.SPEECH_VOTE,
            data_scope=data_scope,
        )
        base = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        base["declaration_quote"] = declaration.exact_quote
        if coverage is not None:
            base["coverage"] = {**self.coverage(), **coverage}
        revision = create_revision(
            dossier=dossier,
            public_payload=base,
            source_snapshot=self.snapshot(scrutiny.source_document),
            title=base["title"],
            short_answer=base["short_answer"],
            main_caveat=base["main_caveat"],
            reasoning=base["reasoning"],
            result=base["result"],
            method_version="DV-M0.1",
            change_summary="Corpus RACC-001",
            comparison=comparison,
        )
        record_controls(revision, deterministic_control_results(revision), environment="test")
        return dossier, revision, comparison

    def _run_php_validate(
        self, envelope: dict, *, expect: str = "accept", reason_prefix: str = ""
    ) -> None:
        contract_test = site_test_script("racc001-contract-test.php")
        if not contract_test.is_file():
            self.fail(f"site_test_missing:{contract_test}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False)
            path = handle.name
        cmd = php_argv(str(contract_test), path, expect)
        if reason_prefix:
            cmd.append(reason_prefix)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        Path(path).unlink(missing_ok=True)
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stderr or result.stdout or "").strip(),
        )

    def _authorized_envelope(self, declaration, scrutiny):
        declaration.position_scope = Declaration.PositionScope.PERSONAL
        declaration.save(update_fields=["position_scope", "updated_at"])
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        reviewer = self.make_user()
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
        validate_contract_envelope(event.payload)
        return event.payload

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_personal_day_and_php_receiver(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        envelope = self._authorized_envelope(declaration, scrutiny)
        public = envelope["public"]
        self.assertEqual(public["compared_actor_scope"], "personal")
        self.assertEqual(public["qualified_declaration"]["precision"], "day")
        self.assertEqual(public["actors"]["compared_actor"]["person_id"], declaration.speaker_person.stable_id)
        self._run_php_validate(envelope)

    def test_collective_spokesperson_compared_organization(self):
        declaration_document = self.make_document(source_id=self.unique("PRESS"))
        passage = EvidencePassage.objects.create(
            document=declaration_document,
            exact_text="Notre groupe soutient la mesure.",
            locator="p1",
            public_url=RACC001_PRESS_PUBLIC_URL,
        )
        person = Person.objects.create(stable_id=self.unique("PERSON"), display_name="Porte-parole")
        group = Organization.objects.create(
            stable_id=self.unique("ORG"), name="Groupe TEST", kind=Organization.Kind.PARLIAMENTARY_GROUP
        )
        declaration = Declaration.objects.create(
            stable_id=self.unique("DECL"),
            speaker_person=person,
            speaker_organization=group,
            speaker_label=person.display_name,
            capacity="porte-parole",
            exact_quote=passage.exact_text,
            occurred_on=date(2024, 6, 15),
            position_scope=Declaration.PositionScope.COLLECTIVE,
            source_passage=passage,
        )
        scrutiny = self.make_scrutiny(self.make_document(source_id=self.unique("OFFICIAL")))
        dossier, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        compared = revision.public_payload["actors"]["compared_actor"]
        self.assertIsNone(compared["person_id"])
        self.assertEqual(compared["organization_id"], group.stable_id)
        self.assertEqual(revision.public_payload["compared_actor_scope"], "collective")

    def test_date_precisions_year_month_unknown(self):
        cases = [
            (Declaration.DatePrecision.YEAR, date(2026, 1, 1), "2026"),
            (Declaration.DatePrecision.MONTH, date(2026, 9, 1), "2026-09"),
        ]
        for precision, occurred_on, expected_value in cases:
            declaration, _, scrutiny, _ = self.make_chain(declaration_date=occurred_on)
            declaration.date_precision = precision
            declaration.save(update_fields=["date_precision", "updated_at"])
            _, revision, _ = self._revision_with_comparison(declaration, scrutiny)
            qualified = revision.public_payload["qualified_declaration"]
            self.assertEqual(qualified["precision"], precision)
            self.assertEqual(qualified["value"], expected_value)
            self.assertEqual(revision.public_payload["declaration_date"], expected_value)

        declaration, _, scrutiny, _ = self.make_chain()
        declaration.occurred_on_state = DataState.UNKNOWN
        declaration.occurred_on = None
        declaration.date_precision = Declaration.DatePrecision.UNKNOWN
        declaration.save(update_fields=["occurred_on_state", "occurred_on", "date_precision", "updated_at"])
        _, revision, _ = self._revision_with_comparison(declaration, scrutiny)
        self.assertIsNone(revision.public_payload["qualified_declaration"]["value"])
        self.assertEqual(revision.public_payload["declaration_date_precision"], "unknown")

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_php_event_schema_bilateral_modes(self):
        for mode in ("legacy", "racc001", "reject"):
            result = subprocess.run(
                php_argv(str(SITE_RACC001_EVENT_SCHEMA_BILATERAL), mode),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_php_receiver_rejects_qualified_date_precision_mismatch(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        envelope = self._authorized_envelope(declaration, scrutiny)
        bad = copy.deepcopy(envelope)
        bad["public"]["qualified_declaration"]["value"] = "2026"
        bad["public"]["qualification_material"]["qualified_declaration"]["value"] = "2026"
        self._run_php_validate(bad, expect="reject", reason_prefix="schema_pattern")

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_php_receiver_rejects_snapshot_digest_mismatch(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        envelope = self._authorized_envelope(declaration, scrutiny)
        bad = copy.deepcopy(envelope)
        bad["authorization"]["snapshot_digest"] = "f" * 64
        self._run_php_validate(bad, expect="reject", reason_prefix="public_snapshot_mismatch")

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_php_receiver_rejects_unknown_schema_version(self):
        declaration, _, scrutiny, _ = self.make_chain(person=True)
        envelope = self._authorized_envelope(declaration, scrutiny)
        bad = copy.deepcopy(envelope)
        bad["schema_version"] = "dv.content/0.9.9-unknown"
        self._run_php_validate(bad, expect="reject", reason_prefix="contract_schema_unsupported")

    @skipUnless(os.environ.get("DV_RACC001_PHP_CONTRACT") == "1", "Contrat PHP historique non demande : executable et scripts recus requis")
    def test_month_precision_passes_php_receiver(self):
        declaration, _, scrutiny, _ = self.make_chain(declaration_date=date(2026, 9, 1))
        declaration.date_precision = Declaration.DatePrecision.MONTH
        declaration.save(update_fields=["date_precision", "updated_at"])
        envelope = self._authorized_envelope(declaration, scrutiny)
        self.assertEqual(envelope["public"]["qualified_declaration"]["value"], "2026-09")
        self._run_php_validate(envelope)


class Racc001SchemaNegativeTests(TestCase):
    def _speech_vote_validator(self):
        schema = publication_schema_for("dv.content/0.4.0-racc001-draft.1")
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def _envelope(self, public: dict) -> dict:
        return {
            "schema_version": "dv.content/0.4.0-racc001-draft.1",
            "policy_version": "DV-PUB-RACC1",
            "record_id": "REC:SCHEMA",
            "record_revision": 1,
            "content_kind": "speech_vote",
            "data_scope": "synthetic",
            "method_version": "DV-M0.1",
            "source_versions": [
                {
                    "source_id": "SRC:1",
                    "version_id": "v1",
                    "sha256": "a" * 64,
                    "public_url": public["declaration_source_url"],
                }
            ],
            "public": public,
            "authorization": {
                "route": "human_reviewed",
                "snapshot_digest": "b" * 64,
                "public_review": {"nature": "revue_humaine", "date": "2026-01-01"},
            },
        }

    def _minimal_public(self, qualified: dict) -> dict:
        return {
            "title": "Titre",
            "short_answer": "Réponse",
            "main_caveat": "Réserve",
            "reasoning": "Raisonnement",
            "result": "inconclusive",
            "declaration_quote": "Citation",
            "declaration_date": qualified.get("value"),
            "declaration_date_state": qualified["state"],
            "declaration_source_url": "https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/exemple",
            "official_text_url": "https://www.assemblee-nationale.fr/dyn/16/textes/exemple",
            "official_vote_url": "https://www.assemblee-nationale.fr/dyn/16/scrutins/exemple",
            "scope": "Périmètre",
            "counterevidence": [],
            "coverage": {
                "period_start": "2024-01-01",
                "period_end": "2024-01-01",
                "expected_state": "unknown",
                "expected_count": None,
                "retrieved_count": 1,
                "normalized_count": 1,
                "publishable_count": 1,
                "exclusions": [],
                "statement": "Test",
            },
            "method_explanation": {
                "attribution": "a",
                "object": "b",
                "scope": "c",
                "chronology": "d",
                "procedure": "e",
                "counterevidence": "f",
            },
            "themes": [],
            "actors": {
                "speaker": {"person_id": "P1", "label": "L", "capacity": "c"},
                "represented_entity": {"organization_id": None},
                "compared_actor": {"person_id": "P1", "organization_id": None, "label": "L"},
                "scope": "personal",
            },
            "declaration_date_precision": qualified["precision"],
            "compared_actor_scope": "personal",
            "qualified_declaration": qualified,
            "qualification_material": {
                "declaration_id": "D1",
                "qualified_declaration": qualified,
                "actors": {
                    "speaker": {"person_id": "P1", "label": "L", "capacity": "c"},
                    "represented_entity": {"organization_id": None},
                    "compared_actor": {"person_id": "P1", "organization_id": None, "label": "L"},
                    "scope": "personal",
                },
                "declaration_measure_links": [],
                "measure_scrutiny_links": [],
                "declaration_source": {
                    "public_url": "https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/exemple",
                    "locator": "p1",
                    "document_version": "v1",
                    "sha256": "a" * 64,
                },
            },
        }

    def test_year_value_rejected_with_day_precision_in_schema(self):
        qualified = {
            "state": "known",
            "precision": "day",
            "value": "2026",
            "interval_start": None,
            "interval_end": None,
        }
        with self.assertRaises(ValidationError):
            validate_contract_envelope(self._envelope(self._minimal_public(qualified)))
        qualified["value"] = "2026-09-17"
        validate_contract_envelope(self._envelope(self._minimal_public(qualified)))

    def test_month_value_accepted_without_iso_date_format(self):
        qualified = {
            "state": "known",
            "precision": "month",
            "value": "2026-09",
            "interval_start": None,
            "interval_end": None,
        }
        validate_contract_envelope(self._envelope(self._minimal_public(qualified)))


@override_settings(DV_ENV="test")
class Racc001SyntheticEnvelopeTests(AtelierTestCase):
    """Enveloppe synthétique minimale lorsque la comparaison n'est pas branchée."""

    def test_export_requires_qualified_fields_on_public(self):
        reviewer = self.make_user()
        public = self.payload(EditorialDossier.Kind.SPEECH_VOTE)
        qualified = {
            "state": "known",
            "precision": "day",
            "value": public["declaration_date"],
            "interval_start": None,
            "interval_end": None,
        }
        actors = {
            "speaker": {"person_id": "SYN:PERSON", "label": "TEST", "capacity": "x"},
            "represented_entity": {"organization_id": None},
            "compared_actor": {"person_id": "SYN:PERSON", "organization_id": None, "label": "TEST"},
            "scope": "personal",
        }
        public.update(
            {
                "actors": actors,
                "compared_actor_scope": "personal",
                "declaration_date_precision": "day",
                "qualified_declaration": qualified,
                "qualification_material": {
                    "declaration_id": "SYN:DECL",
                    "qualified_declaration": qualified,
                    "actors": actors,
                    "declaration_measure_links": [],
                    "measure_scrutiny_links": [],
                    "declaration_source": {
                        "public_url": public["declaration_source_url"],
                        "locator": "test",
                        "document_version": "v1",
                        "sha256": "a" * 64,
                    },
                },
            }
        )
        dossier, revision, _ = self.make_revision(
            EditorialDossier.Kind.SPEECH_VOTE, payload=public, environment="test"
        )
        self.approve(dossier, revision, reviewer)
        with self.final_authority_settings(reviewer):
            event = authorize_and_queue(revision, "test")
        self.assertEqual(event.payload["public"], revision.public_payload)
        validate_contract_envelope(event.payload)
