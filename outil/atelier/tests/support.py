from __future__ import annotations

import hashlib
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from atelier.models import (
    DocumentVersion,
    EditorialDossier,
    HumanReview,
    LegislativeDossier,
    Scrutiny,
    Source,
    TextVersion,
    UserProfile,
)
from atelier.services.calculations import pair_agreement
from atelier.services.publication import record_human_review
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls


class AtelierTestCase(TestCase):
    def setUp(self):
        self.sequence = 0

    def unique(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}:{self.sequence}"

    def make_document(
        self,
        *,
        source_id: str | None = None,
        sha256: str | None = None,
        captured_offset: int = 0,
        source: Source | None = None,
    ) -> DocumentVersion:
        source_id = source_id or self.unique("SRC")
        if source is None:
            path_token = source_id.replace(":", "-").replace("_", "-").lower()
            source = Source.objects.create(
                stable_id=source_id,
                producer="Institution de test",
                family="test",
                discovery_url=f"https://www.assemblee-nationale.fr/{path_token}",
                document_url=f"https://www.assemblee-nationale.fr/{path_token}.json",
                rights_status=Source.RightsStatus.OPEN,
                access_status=Source.AccessStatus.ACTIVE,
                operational_enabled=True,
            )
        digest = sha256 or hashlib.sha256(self.unique("DOC").encode()).hexdigest()
        return DocumentVersion.objects.create(
            source=source,
            version_id=self.unique("v"),
            sha256=digest,
            captured_at=timezone.now() + timedelta(seconds=captured_offset),
            media_type="application/json",
            byte_size=10,
            original_path=f"raw-snapshots/{digest}.json",
            public_copy_allowed=True,
        )

    @staticmethod
    def snapshot(document: DocumentVersion) -> list[dict]:
        return [
            {
                "source_id": document.source.stable_id,
                "version_id": document.version_id,
                "sha256": document.sha256,
                "public_url": document.source.document_url,
            }
        ]

    @staticmethod
    def coverage() -> dict:
        return {
            "period_start": "2024-01-01",
            "period_end": "2024-01-01",
            "expected_state": "unknown",
            "expected_count": None,
            "retrieved_count": 1,
            "normalized_count": 1,
            "publishable_count": 1,
            "exclusions": ["lot de test borné"],
            "statement": "Aucune exhaustivité revendiquée.",
        }

    @staticmethod
    def state_count(value: int | None, state: str = "known", reason: str = "") -> dict:
        result = {"state": state, "value": value}
        if state != "known":
            result["reason"] = reason or "non publié par la source"
        return result

    def make_text(self, document: DocumentVersion, *, relation: str = TextVersion.Relation.SUBMITTED) -> TextVersion:
        dossier = LegislativeDossier.objects.create(
            stable_id=self.unique("LEG-DOSSIER"),
            institution="Assemblée nationale",
            official_title="Dossier officiel",
            official_url="https://www.assemblee-nationale.fr/dyn/16/dossiers/exemple",
            status="en cours",
        )
        return TextVersion.objects.create(
            stable_id=self.unique("TEXT"),
            dossier=dossier,
            kind="projet de loi",
            official_title="Texte officiel soumis",
            version_label="n° test",
            stage="première lecture, version soumise",
            relation=relation,
            occurred_on=date(2024, 1, 1),
            official_url="https://www.assemblee-nationale.fr/dyn/16/textes/exemple",
            source_document=document,
        )

    def make_scrutiny(
        self,
        document: DocumentVersion | None = None,
        *,
        source_identifier: str | None = None,
        institution: str = "Assemblée nationale",
        legislature: str = "16",
        nominative_status: str = Scrutiny.NominativeStatus.AVAILABLE,
        result_status: str = "adopted",
        anomaly: str = "",
    ) -> Scrutiny:
        document = document or self.make_document()
        text = self.make_text(document)
        number = source_identifier or str(self.sequence + 1)
        scrutiny = Scrutiny(
            stable_id=self.unique("SCRUTINY"),
            source_identifier=number,
            institution=institution,
            legislature=legislature,
            session="session test",
            occurred_on=date(2024, 1, 1),
            procedure="scrutin public",
            official_title="Vote sur le texte",
            official_vote_url=f"https://www.assemblee-nationale.fr/dyn/16/scrutins/{number}",
            submitted_text=text,
            result_status=result_status,
            nominative_status=nominative_status,
            registered=None,
            voters=3,
            expressed=2,
            count_for=1,
            count_against=1,
            count_abstention=1,
            source_document=document,
            anomaly=anomaly,
        )
        scrutiny.full_clean()
        scrutiny.save()
        return scrutiny

    def payload(self, kind: str) -> dict:
        text_url = "https://www.assemblee-nationale.fr/dyn/16/textes/l16b1983_projet-loi"
        vote_url = "https://www.assemblee-nationale.fr/dyn/16/scrutins/3289"
        if kind == EditorialDossier.Kind.OFFICIAL_VOTE:
            return {
                "institution": "Assemblée nationale",
                "legislature": "16",
                "session": "2023-2024",
                "official_title": "Vote officiel",
                "scrutin_id": "AN:16:SCRUTIN:3289",
                "occurred_on": "2024-01-30",
                "date_precision": "day",
                "procedure": "scrutin public solennel",
                "stage": "première lecture, version soumise",
                "result_status": "adopted",
                "official_text_url": text_url,
                "official_vote_url": vote_url,
                "text_reference_role": "submitted",
                "nominative_status": "available",
                "counts": {
                    "registered": self.state_count(None, "unknown"),
                    "voters": self.state_count(3),
                    "expressed": self.state_count(2),
                    "for": self.state_count(1),
                    "against": self.state_count(1),
                    "abstention": self.state_count(1),
                },
                "coverage": self.coverage(),
                "themes": [],
            }
        if kind == EditorialDossier.Kind.VOTE_COMPARISON:
            observations = [
                {
                    "scrutin_id": "AN:16:SCRUTIN:3289",
                    "left": "for",
                    "right": "for",
                    "official_text_url": text_url,
                    "official_vote_url": vote_url,
                }
            ]
            return {
                "title": "Comparaison factuelle",
                "scope": "Une paire explicitement bornée.",
                "rule_id": "PAIR_EQUAL_KNOWN_V1",
                "observations": observations,
                "calculation": pair_agreement([{"left": "for", "right": "for"}]),
                "official_text_url": text_url,
                "official_vote_url": vote_url,
                "coverage": self.coverage(),
                "themes": [],
            }
        qualified = {
            "state": "known",
            "precision": "day",
            "value": "2024-01-30",
            "interval_start": None,
            "interval_end": None,
        }
        actors = {
            "speaker": {"person_id": "SYN:PERSON", "label": "Acteur TEST", "capacity": "personnelle"},
            "represented_entity": {"organization_id": None},
            "compared_actor": {"person_id": "SYN:PERSON", "organization_id": None, "label": "Acteur TEST"},
            "scope": "personal",
        }
        return {
            "title": "Comparaison discours-vote",
            "short_answer": "Proposition documentaire à revoir.",
            "main_caveat": "Portée limitée à ce cas.",
            "reasoning": "Les six critères sont explicités ci-dessous.",
            "result": "inconclusive",
            "declaration_quote": "Nous ne promettons pas une conclusion sans preuve.",
            "declaration_date": "2024-01-30",
            "declaration_date_state": "known",
            "declaration_date_precision": "day",
            "declaration_source_url": "https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/exemple",
            "official_text_url": text_url,
            "official_vote_url": vote_url,
            "scope": "Acteur et scrutin explicitement bornés.",
            "counterevidence": [],
            "coverage": self.coverage(),
            "method_explanation": {
                "attribution": "Acteur identifié.",
                "object": "Objet explicite.",
                "scope": "Portée explicite.",
                "chronology": "Chronologie vérifiée.",
                "procedure": "Procédure vérifiée.",
                "counterevidence": "Recherche documentée, résultat vide admis.",
            },
            "themes": [],
            "actors": actors,
            "compared_actor_scope": "personal",
            "qualified_declaration": qualified,
            "qualification_material": {
                "declaration_id": "SYN:DECL",
                "qualified_declaration": qualified,
                "actors": actors,
                "declaration_measure_links": [],
                "measure_scrutiny_links": [],
                "declaration_source": {
                    "public_url": "https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/exemple",
                    "locator": "fixture",
                    "document_version": "v-test",
                    "sha256": "d" * 64,
                },
            },
        }

    def make_revision(
        self,
        kind: str = EditorialDossier.Kind.OFFICIAL_VOTE,
        *,
        document: DocumentVersion | None = None,
        data_scope: str = "synthetic",
        payload: dict | None = None,
        environment: str = "test",
    ):
        document = document or self.make_document()
        dossier = EditorialDossier.objects.create(
            stable_id=self.unique("EDITORIAL"),
            content_kind=kind,
            established_kind=kind,
            data_scope=data_scope,
        )
        public = payload or self.payload(kind)
        revision = create_revision(
            dossier=dossier,
            public_payload=public,
            source_snapshot=self.snapshot(document),
            title=public.get("title") or public.get("official_title") or "Titre",
            short_answer=public.get("short_answer", "Fait officiel."),
            main_caveat=public.get("main_caveat", "Périmètre borné."),
            reasoning=public.get("reasoning", "Contrôle documentaire."),
            result=public.get("result", ""),
            method_version=("" if kind == EditorialDossier.Kind.OFFICIAL_VOTE else "DV-M0.1"),
            change_summary="Création de test.",
        )
        record_controls(revision, deterministic_control_results(revision), environment=environment)
        return dossier, revision, document

    def make_user(self, role: str = UserProfile.Role.REVIEWER, *, test_identity: bool = True):
        username = self.unique("user").replace(":", "-")
        user = get_user_model().objects.create_user(username=username, password="Long-Test-Password-123!")
        UserProfile.objects.create(
            user=user,
            role=role,
            active=True,
            is_test_identity=test_identity,
            login_pseudonym=username,
            public_attribution_label=username,
            account_status=UserProfile.AccountStatus.ACTIVE,
            must_change_password=False,
        )
        return user

    def final_authority_settings(self, user):
        return override_settings(
            DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=str(user.dv_profile.internal_uid)
        )

    def approve(self, dossier, revision, user, environment="test"):
        with self.final_authority_settings(user):
            return record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=user,
                decision=HumanReview.Decision.APPROVE,
                environment=environment,
            )

    def approve_and_authorize(self, dossier, revision, user, environment="test"):
        """Approuve puis autorise sous la même autorité finale configurée."""
        from atelier.services.publication import authorize_and_queue

        with self.final_authority_settings(user):
            review = record_human_review(
                dossier_id=dossier.pk,
                expected_revision=revision.revision,
                expected_digest=revision.snapshot_digest,
                user=user,
                decision=HumanReview.Decision.APPROVE,
                environment=environment,
            )
            event = authorize_and_queue(revision, environment)
        return review, event
