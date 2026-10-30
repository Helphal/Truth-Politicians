from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from atelier.models import ControlRecord, EditorialDossier, EditorialRevision, Environment

from .canonical import sha256_digest
from .factual_checks import counts_check, calculation_check
from .catalogue import prepare_public_snapshot, binding_check
from .publication import REQUIRED_CONTROLS, validate_public_payload, validate_source_snapshot


@transaction.atomic
def create_revision(*, dossier: EditorialDossier, public_payload: dict, source_snapshot: list,
                    title: str, short_answer: str, main_caveat: str, reasoning: str,
                    result: str = "", method_version: str = "", change_summary: str = "", created_by=None,
                    comparison=None, conclusion_contract=None):
    dossier = EditorialDossier.objects.select_for_update().get(pk=dossier.pk)
    dossier.full_clean()
    needs_prepare = dossier.data_scope == "real" or (
        dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE and comparison is not None
    )
    if needs_prepare:
        public_payload, source_snapshot = prepare_public_snapshot(
            dossier.content_kind, public_payload, source_snapshot, comparison=comparison
        )
    validate_public_payload(dossier.content_kind, public_payload)
    validate_source_snapshot(source_snapshot)
    if (public_payload.get('public_contract_version') == 'dv.content/0.7.0-conclusion-draft.1'
            and not conclusion_contract):
        raise ValidationError('classification_conclusion_required_for_public_profile')
    number = dossier.current_revision + 1
    revision = EditorialRevision.objects.create(
        dossier=dossier,
        revision=number,
        comparison=comparison,
        title=title,
        short_answer=short_answer,
        main_caveat=main_caveat,
        reasoning=reasoning,
        result=result,
        method_version=method_version,
        source_snapshot=source_snapshot,
        public_payload=public_payload,
        conclusion_contract=conclusion_contract or {},
        snapshot_digest=sha256_digest(public_payload),
        change_summary=change_summary,
        created_by=created_by,
        # Les relevés/calculs structurés conservent leurs contrôles déterministes.
        # Les nouvelles interprétations réelles exigent aussi l'examen documentaire.
        verification_required=dossier.data_scope == 'real' and dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE,
    )
    dossier.current_revision = number
    dossier.state = EditorialDossier.State.READY_FOR_REVIEW if dossier.content_kind == EditorialDossier.Kind.SPEECH_VOTE else EditorialDossier.State.DRAFT
    dossier.save(update_fields=["current_revision", "state", "updated_at"])
    if revision.conclusion_contract:
        from .conclusion_dependencies import validate_conclusion
        from .verification import snapshot
        validate_conclusion(snapshot(revision), revision.conclusion_contract)
    if revision.verification_required:
        from .verification import enqueue
        enqueue(revision)
    return revision


def record_controls(revision: EditorialRevision, results: dict[str, tuple[bool, str]], *,
                    environment: str = Environment.TEST, actor: str = "dv-control-engine/1") -> list[ControlRecord]:
    required = REQUIRED_CONTROLS[revision.dossier.content_kind]
    unknown = set(results) - required
    if unknown:
        raise ValidationError(f"Contrôles inattendus: {sorted(unknown)}")
    records = []
    for name in sorted(required):
        passed, evidence = results.get(name, (False, "not_executed"))
        record, _ = ControlRecord.objects.update_or_create(
            revision=revision,
            control_type=name,
            rule_version="DV-CONTROL-1",
            environment=environment,
            defaults={
                "input_digest": revision.snapshot_digest,
                "passed": bool(passed),
                "anomaly_code": "" if passed else evidence[:100],
                "evidence": evidence,
                "technical_actor": actor,
            },
        )
        records.append(record)
    return records


def deterministic_control_results(revision: EditorialRevision) -> dict[str, tuple[bool, str]]:
    """Contrôles mécaniques ; aucun de ces résultats ne vaut revue politique."""
    payload = revision.public_payload
    try:
        validate_public_payload(revision.dossier.content_kind, payload)
        privacy_ok = True
    except ValidationError:
        privacy_ok = False
    source_ok, source_evidence = binding_check(revision)
    from atelier.models import DocumentVersion, Source
    documents = [DocumentVersion.objects.filter(source__stable_id=item.get("source_id"), version_id=item.get("version_id"), sha256=item.get("sha256")).select_related("source").first() for item in revision.source_snapshot]
    reuse_ok = bool(documents) and all(doc and doc.source.rights_status in {Source.RightsStatus.OPEN, Source.RightsStatus.EXCERPT_ALLOWED} for doc in documents)
    if revision.dossier.data_scope == "real":
        reuse_ok = reuse_ok and all(doc.source.qualified_at and doc.source.terms_url for doc in documents)
    if revision.conclusion_contract:
        from .proof_architecture import received_rights_for_render
        reuse_ok = received_rights_for_render(revision, 'public_excerpt') is not None
    results = {
        "source_fidelity": (source_ok, source_evidence),
        "identity": (source_ok, "Identifiants liés aux objets normalisés; fixtures non probantes" if source_ok else "identity_not_bound"),
        "procedure": (source_ok, "Procédure et objet comparés au référentiel; pas une revue politique" if source_ok else "procedure_not_bound"),
        "coverage": ("coverage" in payload, "couverture présente" if "coverage" in payload else "coverage_missing"),
        "public_sources": (bool(payload.get("official_text_url") and payload.get("official_vote_url")), "deux liens présents"),
        "reuse": (reuse_ok, "Qualification de réutilisation conservée par source; pas une validation juridique" if reuse_ok else "reuse_not_qualified"),
        "privacy": (privacy_ok, "Liste blanche et filtrage des liens exécutés; ne certifie pas toute donnée libre" if privacy_ok else "public_payload_filter_failed"),
    }
    kind = revision.dossier.content_kind
    if kind == EditorialDossier.Kind.OFFICIAL_VOTE:
        results["counts"] = counts_check(payload)
    elif kind == EditorialDossier.Kind.VOTE_COMPARISON:
        results["calculation"] = calculation_check(payload)
    else:
        criteria = payload.get("method_explanation", {})
        results["comparability"] = (bool(criteria), "critères documentés" if criteria else "criteria_missing")
        results["citations"] = (bool(payload.get("declaration_quote")), "citation et source présentes")
        results["counterevidence"] = ("counterevidence" in payload, "contre-éléments explicités")
    return results
