"""Fixture base TEST pour chaîne HTTP Outil → WordPress (RECETTE-01/02/03/04)."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from atelier.models import Declaration, EditorialDossier, Mandate, Person, UserProfile, VoteObservation
from atelier.services.accounts import create_account
from atelier.services.catalogue import official_fields, prepare_public_snapshot
from atelier.services.revisions import create_revision, deterministic_control_results, record_controls
from atelier.tests.racc001_local_b_live import LocalBLiveSupport
from atelier.tests.test_racc001_corpus import Racc001CorpusTests


class Command(BaseCommand):
    help = "Titulaire + dossiers OFFICIAL_VOTE (01), VOTE_COMPARISON (02), personnel (03), collectif (04)."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--run-id", default="")

    def handle(self, *args, **options):
        run_id = (options["run_id"] or os.environ.get("DV_RACC001_RUN_ID", "")).strip()
        if not run_id:
            run_id = secrets.token_hex(4)
        os.environ["DV_RACC001_RUN_ID"] = run_id
        os.environ["DV_RACC001_RUN_NONCE"] = secrets.token_hex(4)

        manifest_path = Path(options["manifest"]).resolve()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        holder_pw = secrets.token_urlsafe(24)
        holder_cred = create_account(role=UserProfile.Role.ADMIN, is_test_identity=True)
        holder = get_user_model().objects.get(pk=holder_cred.user_id)
        holder.set_password(holder_pw)
        holder.dv_profile.must_change_password = False
        holder.dv_profile.save()
        holder.save()

        class _Harness(LocalBLiveSupport, Racc001CorpusTests):
            pass

        harness = _Harness()
        harness.setUp()

        doc01 = harness.make_document()
        doc01.source.qualified_at = timezone.now()
        doc01.source.terms_url = "https://www.assemblee-nationale.fr/synthetic-terms"
        doc01.source.save()
        scrutiny01 = harness.make_scrutiny(doc01)
        alice = Person.objects.create(
            stable_id=harness.unique("SYN:PERSON-A"),
            display_name="Personne fictive Alpha",
        )
        beta = Person.objects.create(
            stable_id=harness.unique("SYN:PERSON-B"),
            display_name="Personne fictive Bêta",
        )
        mandate = Mandate.objects.create(
            stable_id=harness.unique("SYN:MANDATE"),
            person=beta,
            institution=scrutiny01.institution,
            mandate_type="mandat fictif",
            territory="territoire fictif",
            starts_on=date(2020, 1, 1),
            source_document=doc01,
        )
        for person, category in ((alice, "for"), (beta, "against")):
            observation_row = VoteObservation(
                scrutiny=scrutiny01,
                person=person,
                mandate=mandate if person == beta else None,
                source_category=category,
                normalized_category=category,
                mapping_rule="SYNTHETIC_MAPPING",
                source_document=doc01,
            )
            observation_row.full_clean()
            observation_row.save()
        source_snapshot = harness.snapshot(doc01)
        public01, source_snapshot = prepare_public_snapshot(
            EditorialDossier.Kind.OFFICIAL_VOTE,
            {**official_fields(scrutiny01), "coverage": harness.coverage(), "themes": []},
            source_snapshot,
        )
        dossier01 = EditorialDossier.objects.create(
            stable_id=harness.unique("OFFICIAL"),
            content_kind=EditorialDossier.Kind.OFFICIAL_VOTE,
            established_kind=EditorialDossier.Kind.OFFICIAL_VOTE,
            data_scope="synthetic",
        )
        revision01 = create_revision(
            dossier=dossier01,
            public_payload=public01,
            source_snapshot=source_snapshot,
            title=public01.get("official_title") or "Vote officiel TEST",
            short_answer="Fait officiel borné.",
            main_caveat="Données entièrement fictives.",
            reasoning="Catalogue matérialisé pour dépendances vote-vote.",
            change_summary="Création fait officiel 01 avec catalogue.",
        )
        record_controls(revision01, deterministic_control_results(revision01), environment="test")
        if "catalogue" not in revision01.public_payload:
            raise RuntimeError("recette01_catalogue_missing")

        from atelier.services.calculations import pair_agreement

        text_url = scrutiny01.submitted_text.official_url
        vote_url = scrutiny01.official_vote_url
        basis_for = {"kind": "reference_category", "category": "for"}
        basis_not_recorded = {"kind": "reference_category", "category": "not_recorded"}
        payload02 = harness.payload(EditorialDossier.Kind.VOTE_COMPARISON)
        payload02["observations"] = [
            {
                "scrutin_id": scrutiny01.stable_id,
                "left": "for",
                "right": "for",
                "left_person_id": alice.stable_id,
                "right_person_id": alice.stable_id,
                "official_text_url": text_url,
                "official_vote_url": vote_url,
            },
            {
                "scrutin_id": scrutiny01.stable_id,
                "left": "for",
                "right": "against",
                "left_person_id": alice.stable_id,
                "right_person_id": beta.stable_id,
                "official_text_url": text_url,
                "official_vote_url": vote_url,
            },
            {
                "scrutin_id": scrutiny01.stable_id,
                "left": "for",
                "right": "not_recorded",
                "official_text_url": text_url,
                "official_vote_url": vote_url,
                "left_basis": basis_for,
                "right_basis": basis_not_recorded,
            },
        ]
        payload02["calculation"] = pair_agreement(payload02["observations"])
        payload02["official_text_url"] = scrutiny01.submitted_text.official_url
        payload02["official_vote_url"] = scrutiny01.official_vote_url
        public02, source_snapshot02 = prepare_public_snapshot(
            EditorialDossier.Kind.VOTE_COMPARISON,
            payload02,
            source_snapshot,
        )
        dossier02 = EditorialDossier.objects.create(
            stable_id=harness.unique("VOTECMP"),
            content_kind=EditorialDossier.Kind.VOTE_COMPARISON,
            established_kind=EditorialDossier.Kind.VOTE_COMPARISON,
            data_scope="synthetic",
        )
        revision02 = create_revision(
            dossier=dossier02,
            public_payload=public02,
            source_snapshot=source_snapshot02,
            title=public02["title"],
            short_answer="Comparaison factuelle bornée.",
            main_caveat="Périmètre limité au scrutin de référence.",
            reasoning="Calcul et bases explicités.",
            method_version="DV-M0.1",
            change_summary="Création comparaison vote-vote alignée sur le fait officiel 01.",
        )
        record_controls(revision02, deterministic_control_results(revision02), environment="test")
        calc = revision02.public_payload.get("calculation") or {}
        if calc.get("denominator", 0) != 2 or calc.get("numerator", 0) != 1 or calc.get("excluded", 0) != 1:
            raise RuntimeError("recette02_calculation_mismatch")

        declaration03, _, scrutiny03, _ = harness.make_chain(person=True)
        declaration03.position_scope = Declaration.PositionScope.PERSONAL
        declaration03.save(update_fields=["position_scope", "updated_at"])
        dossier03, revision03, _ = harness._revision_with_comparison(declaration03, scrutiny03)
        dossier03.stable_id = harness.unique("PERSONAL")
        dossier03.save(update_fields=["stable_id", "updated_at"])

        dossier04, revision04, declaration04, group04 = harness._make_collective_month_chain()
        dossier04.stable_id = harness.unique("COLLECTIVE")
        dossier04.save(update_fields=["stable_id", "updated_at"])
        dist = revision04.public_payload.get("collective_distribution") or {}
        if revision04.public_payload.get("compared_actor_scope") != "collective":
            raise RuntimeError("recette04_not_collective")

        manifest = {
            "marker": "DV_RACC_001_HTTP_CHAIN_WP",
            "run_id": run_id,
            "holder_username": holder.username,
            "holder_password": holder_pw,
            "holder_internal_uid": str(holder.dv_profile.internal_uid),
            "recette_01": {
                "content_kind": dossier01.content_kind,
                "dossier_id": dossier01.pk,
                "record_id": dossier01.stable_id,
                "revision": revision01.revision,
                "snapshot_digest": revision01.snapshot_digest,
                "official_title": revision01.public_payload.get("official_title", ""),
                "official_text_url": revision01.public_payload["official_text_url"],
                "official_vote_url": revision01.public_payload["official_vote_url"],
            },
            "recette_02": {
                "content_kind": dossier02.content_kind,
                "dossier_id": dossier02.pk,
                "record_id": dossier02.stable_id,
                "revision": revision02.revision,
                "snapshot_digest": revision02.snapshot_digest,
                "rule_id": revision02.public_payload.get("rule_id", ""),
                "calculation_numerator": calc.get("numerator"),
                "calculation_denominator": calc.get("denominator"),
                "calculation_excluded": calc.get("excluded"),
                "left_person_name": alice.display_name,
                "right_person_name": beta.display_name,
                "observation_count": len(revision02.public_payload.get("observations") or []),
                "official_text_url": revision02.public_payload["official_text_url"],
                "official_vote_url": revision02.public_payload["official_vote_url"],
            },
            "recette_03": {
                "content_kind": dossier03.content_kind,
                "dossier_id": dossier03.pk,
                "record_id": dossier03.stable_id,
                "revision": revision03.revision,
                "snapshot_digest": revision03.snapshot_digest,
                "declaration_quote": revision03.public_payload["declaration_quote"],
                "compared_actor_scope": revision03.public_payload["compared_actor_scope"],
            },
            "recette_04": {
                "content_kind": dossier04.content_kind,
                "dossier_id": dossier04.pk,
                "record_id": dossier04.stable_id,
                "revision": revision04.revision,
                "snapshot_digest": revision04.snapshot_digest,
                "speaker_name": declaration04.speaker_person.display_name,
                "organization_name": group04.name,
                "collective_distribution_state": dist.get("state"),
                "collective_rule_id": dist.get("rule_id"),
                "collective_as_of": dist.get("as_of"),
                "excluded_count": len(dist.get("excluded_member_ids") or []),
                "included_count": len(dist.get("included_member_ids") or []),
                "declaration_quote": revision04.public_payload["declaration_quote"],
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "manifest": str(manifest_path),
                    "run_id": run_id,
                    "holder_internal_uid": manifest["holder_internal_uid"],
                }
            )
        )
