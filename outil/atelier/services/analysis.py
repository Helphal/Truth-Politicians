"""Adaptateurs de préparation : documentaire local ou fournisseur explicitement bloqué."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from atelier.models import JobRun

from .canonical import sha256_digest
from .jobs import claim_job, finish_job
from .analysis_provider import AnalysisProviderUnavailable, prepare as prepare_with_analysis_provider


PROCESSOR_VERSION = "dv-documentary-preparation/1"
ANALYSIS_PROVIDER_PROCESSOR_VERSION = "dv-analysis_provider-preparation/1"


class ProviderBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparationResult:
    job_id: str
    state: str
    cached: bool
    provider_calls: int
    cost_eur: Decimal | None
    proposal: dict


class DeterministicDocumentaryAdapter:
    """Valide et recopie un brouillon structuré sans produire d'inférence politique."""

    name = "deterministic_documentary"

    @staticmethod
    def prepare(candidate: dict) -> dict:
        if not isinstance(candidate, dict) or set(candidate) != {"draft", "evidence_refs", "method"}:
            raise ValueError("documentary_candidate_invalid")
        if not isinstance(candidate["draft"], dict) or not candidate["draft"]:
            raise ValueError("documentary_draft_missing")
        if not isinstance(candidate["evidence_refs"], list) or not candidate["evidence_refs"]:
            raise ValueError("documentary_evidence_missing")
        if not isinstance(candidate["method"], str) or not candidate["method"]:
            raise ValueError("documentary_method_missing")
        return copy.deepcopy(candidate["draft"])


def prepare_documentary(*, batch_id: str, candidate: dict) -> PreparationResult:
    configuration = {
        "adapter": DeterministicDocumentaryAdapter.name,
        "provider": "none",
        "network_calls": 0,
        "fallback": "none",
        "cost_currency": "EUR",
    }
    input_digest = sha256_digest({"candidate": candidate, "configuration": configuration})
    job, _ = JobRun.objects.get_or_create(
        task_name="editorial_preparation",
        batch_id=batch_id,
        input_digest=input_digest,
        processor_version=PROCESSOR_VERSION,
        defaults={
            "configuration": configuration,
            "cost_limit": Decimal("0"),
            "cost_units": Decimal("0"),
        },
    )
    cached = job.state == JobRun.State.SUCCEEDED
    proposal = DeterministicDocumentaryAdapter.prepare(candidate)
    if not cached:
        claimed = claim_job(job.job_id)
        if claimed is None:
            raise RuntimeError("documentary_job_not_claimable")
        with transaction.atomic():
            job = JobRun.objects.select_for_update().get(pk=claimed.pk)
            job.configuration = {
                **configuration,
                "result_digest": sha256_digest(proposal),
            }
            job.save(update_fields=["configuration", "updated_at"])
        job = finish_job(job.job_id, succeeded=True)
    else:
        job.refresh_from_db()
        if job.configuration.get("result_digest") != sha256_digest(proposal):
            raise RuntimeError("documentary_cache_digest_mismatch")
    return PreparationResult(
        job_id=str(job.job_id),
        state=str(job.state),
        cached=cached,
        provider_calls=0,
        cost_eur=job.cost_units,
        proposal=proposal,
    )


def prepare_with_external_provider(*, batch_id: str, candidate: dict) -> PreparationResult:
    """Prépare via le service configuré, sans repli ni approbation implicite."""
    configuration = {
        "adapter": "analysis_provider_private_cognitive",
        "provider": "analysis_provider",
        "network_calls": 1,
        "fallback": "none",
        "endpoint": "private_configured" if bool(settings.DV_ANALYSIS_PROVIDER_ENDPOINT) else "missing",
        "human_review_created": False,
        "publication_authorized": False,
    }
    input_digest = sha256_digest({"candidate": candidate, "configuration": configuration})
    job, _ = JobRun.objects.get_or_create(
        task_name="editorial_preparation_external",
        batch_id=batch_id,
        input_digest=input_digest,
        processor_version=ANALYSIS_PROVIDER_PROCESSOR_VERSION,
        defaults={
            "configuration": configuration,
            "cost_limit": Decimal("0"),
            "cost_units": Decimal("0"),
        },
    )
    if job.state == JobRun.State.SUCCEEDED:
        proposal = job.configuration.get("proposal")
        if not isinstance(proposal, dict) or job.configuration.get("result_digest") != sha256_digest(proposal):
            raise RuntimeError("analysis_provider_cache_digest_mismatch")
        return PreparationResult(
            job_id=str(job.job_id),
            state=str(job.state),
            cached=True,
            provider_calls=0,
            cost_eur=Decimal(str(job.configuration["cost_eur"])) if job.configuration.get("cost_reported") else None,
            proposal=proposal,
        )
    claimed = claim_job(job.job_id)
    if claimed is None:
        raise RuntimeError("analysis_provider_job_not_claimable")
    try:
        result = prepare_with_analysis_provider(candidate)
    except AnalysisProviderUnavailable as exc:
        with transaction.atomic():
            job = JobRun.objects.select_for_update().get(pk=claimed.pk)
            job.state = JobRun.State.BLOCKED
            job.error_code = str(exc)[:100]
            job.lease_until = None
            job.configuration = {**configuration, "blocked_reason": str(exc)}
            job.save(update_fields=["state", "error_code", "lease_until", "configuration", "updated_at"])
        raise ProviderBlocked(str(exc)) from None
    with transaction.atomic():
        job = JobRun.objects.select_for_update().get(pk=claimed.pk)
        job.configuration = {
            **configuration,
            "model": result.model,
            "reported_provider": result.provider,
            "usage": result.usage,
            "proposal": result.proposal,
            "result_digest": sha256_digest(result.proposal),
            "cost_reported": result.cost_eur is not None,
            "cost_eur": str(result.cost_eur) if result.cost_eur is not None else None,
        }
        if result.cost_eur is not None:
            job.cost_units = result.cost_eur
        job.save(update_fields=["configuration", "cost_units", "updated_at"])
    job = finish_job(job.job_id, succeeded=True)
    return PreparationResult(
        job_id=str(job.job_id),
        state=str(job.state),
        cached=False,
        provider_calls=1,
        cost_eur=result.cost_eur,
        proposal=result.proposal,
    )
