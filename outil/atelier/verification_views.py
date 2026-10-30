from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Subquery
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import EditorialRevision, UserProfile, VerificationJob
from .services.verification import enqueue, deterministic, gate
from .services.verification_review import build_review, summary_review
from .services.verification_architecture_review import build_architecture_review
from .services.publication import _validate_public_url
from .views import permission_required


def _reader(user):
    profile = getattr(user, 'dv_profile', None)
    if not profile or not profile.is_account_usable() or profile.role not in {
        UserProfile.Role.ADMIN, UserProfile.Role.EDITOR, UserProfile.Role.REVIEWER,
        UserProfile.Role.OPERATOR, UserProfile.Role.VERIFIER}:
        raise PermissionDenied('verification_read_forbidden')


def _assessment(revision):
    from .services.proof_architecture import assessment_for_revision
    return assessment_for_revision(revision)


def _history(job):
    rows = VerificationJob.objects.filter(revision__dossier_id=job.revision.dossier_id).select_related(
        'revision', 'receipt').order_by('-revision__revision', '-pk')
    return [{'job_id': row.pk, 'revision': row.revision.revision, 'state': row.state_label,
             'fingerprint': row.fingerprint,
             'decision': row.receipt.decision if hasattr(row, 'receipt') else 'Aucun reçu documentaire'} for row in rows]


@login_required
def index(request):
    _reader(request.user)
    jobs = VerificationJob.objects.select_related('revision__dossier').order_by('-created_at', '-pk')
    history = request.GET.get('history') == '1'
    if not history:
        latest = VerificationJob.objects.filter(revision_id=OuterRef('revision_id')).order_by('-pk')
        jobs = jobs.filter(revision__revision=F('revision__dossier__current_revision'), pk=Subquery(latest.values('pk')[:1]))
    page = Paginator(jobs, 25).get_page(request.GET.get('page'))
    for job in page.object_list:
        job.review_axes = summary_review(job, list(job.attempts.order_by('-number')), gate(job.revision))
        job.architecture_review = build_architecture_review(job, _assessment(job.revision))
    return render(request, 'atelier/verification.html', {'page': page, 'history': history})


@login_required
def detail(request, pk):
    _reader(request.user)
    job = get_object_or_404(VerificationJob.objects.select_related('revision__dossier'), pk=pk)
    import copy
    sources = copy.deepcopy(job.input_snapshot['sources'])
    from .models import Source
    rights = {s.stable_id:s for s in Source.objects.filter(stable_id__in=[s.get('reference',{}).get('source_id') for s in sources])}
    for source in sources:
        assessed = rights.get(source.get('reference',{}).get('source_id'))
        if assessed:
            source['rights_label'] = assessed.get_rights_status_display()
            source['rights_assessment'] = assessed.qualification_notes
            source['licence_label'] = assessed.licence_label
            try:
                _validate_public_url(assessed.terms_url); source['terms_url'] = assessed.terms_url
            except ValidationError:
                source['terms_url'] = ''
        for passage in source.get('passages', []):
            try:
                _validate_public_url(passage.get('public_url'))
            except ValidationError:
                passage['public_url'] = ''
    attempts = list(job.attempts.order_by('-number'))
    eligibility = gate(job.revision)
    review = build_review(job, attempts, sources, eligibility)
    return render(request, 'atelier/verification_detail.html', {
        'job': job, 'checks': deterministic(job), 'eligibility': eligibility,
        'attempts': attempts, 'sources': sources,
        'review': review,
        'architecture': build_architecture_review(job, _assessment(job.revision), query=request.GET,
                                                  history=_history(job), legacy_review=review),
        'claims': job.input_snapshot['claims']})


@login_required
@permission_required('editorial.review.prepare')
@require_POST
def request_verification(request, pk):
    revision = get_object_or_404(EditorialRevision, pk=pk)
    if revision.dossier.current_revision != revision.revision:
        return HttpResponse('Révision périmée.', status=409)
    # Demander un examen ne permet jamais de fournir son verdict ni son rôle.
    if set(request.POST) - {'csrfmiddlewaretoken', 'fingerprint'}:
        return HttpResponse('Paramètre inattendu.', status=400)
    revision.verification_required = True
    revision.save(update_fields=['verification_required'])
    return redirect('verification_detail', pk=enqueue(revision).pk)
