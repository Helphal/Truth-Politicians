from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from atelier.models import JobRun


class JobLimitReached(RuntimeError):
    pass


@transaction.atomic
def claim_job(job_id, *, lease_seconds: int = 300) -> JobRun | None:
    job = JobRun.objects.select_for_update().get(job_id=job_id)
    now = timezone.now()
    if job.state == JobRun.State.RUNNING and job.lease_until and job.lease_until > now:
        return None
    if job.state in {JobRun.State.SUCCEEDED, JobRun.State.CANCELLED}:
        return None
    job.state = JobRun.State.RUNNING
    job.attempt += 1
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.save(update_fields=["state", "attempt", "lease_until", "updated_at"])
    return job


def add_cost(job_id, units: Decimal) -> JobRun:
    limit_reached = False
    with transaction.atomic():
        job = JobRun.objects.select_for_update().get(job_id=job_id)
        candidate = job.cost_units + units
        if job.cost_limit and candidate > job.cost_limit:
            job.state = JobRun.State.BLOCKED
            job.error_code = "cost_limit_reached"
            job.lease_until = None
            job.save(update_fields=["state", "error_code", "lease_until", "updated_at"])
            limit_reached = True
        else:
            job.cost_units = candidate
            job.save(update_fields=["cost_units", "updated_at"])
    # Lever après validation de la transaction : l'état bloqué doit survivre à
    # l'exception afin qu'une reprise ne tente pas un fournisseur de secours.
    if limit_reached:
        raise JobLimitReached("cost_limit_reached")
    return job


@transaction.atomic
def finish_job(job_id, *, succeeded: bool, error_code: str = "") -> JobRun:
    job = JobRun.objects.select_for_update().get(job_id=job_id)
    job.state = JobRun.State.SUCCEEDED if succeeded else JobRun.State.FAILED
    job.error_code = "" if succeeded else error_code[:100]
    job.lease_until = None
    job.save(update_fields=["state", "error_code", "lease_until", "updated_at"])
    return job
