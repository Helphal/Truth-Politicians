from __future__ import annotations

import difflib
import json
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import (
    CorrectionForm,
    DeclarationMeasureQualificationForm,
    MeasureScrutinyQualificationForm,
    ReviewActionForm,
)
from .models import (
    Declaration,
    DeclarationMeasureLink,
    EditorialDossier,
    Environment,
    Measure,
    MeasureScrutinyLink,
    OutboxEvent,
    Scrutiny,
    Source,
    UserProfile,
)
from .services.publication import authorize_and_queue, evaluate_revision, record_human_review
from .services.revisions import create_revision, deterministic_control_results, record_controls


def _runtime_environment() -> str:
    return {
        "local": Environment.TEST,
        "test": Environment.TEST,
        "private": Environment.PRIVATE,
        "production": Environment.PRODUCTION,
    }[settings.DV_ENV]


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                profile = request.user.dv_profile
            except UserProfile.DoesNotExist:
                return HttpResponseForbidden("Rôle applicatif absent")
            if not profile.is_account_usable():
                return HttpResponseForbidden("Compte indisponible")
            from atelier.services.accounts import role_satisfies

            if not role_satisfies(profile.role, roles):
                return HttpResponseForbidden("Accès refusé pour ce rôle")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def permission_required(permission: str):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                profile = request.user.dv_profile
            except UserProfile.DoesNotExist:
                return HttpResponseForbidden("Rôle applicatif absent")
            from atelier.services.accounts import profile_has_permission

            if not profile_has_permission(profile, permission):
                return HttpResponseForbidden("Permission refusée")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


class SecureLoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            from atelier.services.login_throttle import is_login_throttled

            if is_login_throttled(request):
                return render(
                    request,
                    "registration/login.html",
                    {"throttled": True},
                    status=429,
                )
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        from atelier.services.account_audit import record_account_audit
        from atelier.services.login_throttle import record_login_failure

        record_login_failure(self.request)
        record_account_audit(
            actor=None,
            action="login",
            target_user=None,
            success=False,
            reason="credentials_rejected",
        )
        return super().form_invalid(form)

    def form_valid(self, form):
        from atelier.services.login_throttle import clear_login_throttle

        clear_login_throttle(self.request)
        user = form.get_user()
        from atelier.services.account_audit import record_account_audit
        from atelier.services.accounts import bind_session_epoch, touch_activity
        from atelier.services.private_admin_entry import (
            bind_admin_private_session,
            is_admin_account,
            is_private_admin_entry,
            refuse_admin_on_public,
        )

        try:
            profile = user.dv_profile
        except UserProfile.DoesNotExist:
            profile = None
        if profile is not None and not profile.is_account_usable():
            from django.contrib.auth import logout

            # Ne pas authentifier un compte inutilisable.
            record_account_audit(
                actor=None,
                action="login",
                target_user=user,
                success=False,
                reason="account_not_usable",
            )
            return render(
                self.request,
                "registration/login.html",
                {"form": self.get_form_class()(), "account_blocked": True},
                status=403,
            )
        if is_admin_account(user) and not is_private_admin_entry(self.request):
            return refuse_admin_on_public(
                self.request,
                reason="admin_login_public_refused",
                target_user=user,
            )

        response = super().form_valid(form)
        bind_session_epoch(self.request, user)
        touch_activity(user)
        if is_admin_account(user):
            bind_admin_private_session(self.request)
        record_account_audit(
            actor=user,
            action="login",
            target_user=user,
            success=True,
            reason="ok",
        )
        if profile is not None and profile.must_change_password:
            return redirect("password_change_required")
        return response


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def dashboard(request):
    return render(
        request,
        "atelier/dashboard.html",
        {
            "source_count": Source.objects.count(),
            "scrutiny_count": Scrutiny.objects.count(),
            "dossier_count": EditorialDossier.objects.count(),
            "pending_count": EditorialDossier.objects.filter(state=EditorialDossier.State.READY_FOR_REVIEW).count(),
        },
    )


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def sources(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    rows = Source.objects.all()
    if query:
        rows = rows.filter(Q(stable_id__icontains=query) | Q(producer__icontains=query) | Q(family__icontains=query))
    if status:
        rows = rows.filter(access_status=status)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    for source in page.object_list:
        source.latest_coverage = source.coverage_records.order_by("-period_end", "-updated_at").first()
    return render(request, "atelier/sources.html", {"page": page, "query": query, "status": status})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def facts(request):
    query = request.GET.get("q", "").strip()
    institution = request.GET.get("institution", "").strip()
    rows = Scrutiny.objects.filter(identity_correction__isnull=True).select_related("submitted_text").order_by("-occurred_on", "institution")
    if query:
        rows = rows.filter(Q(official_title__icontains=query) | Q(source_identifier__icontains=query))
    if institution:
        rows = rows.filter(institution=institution)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return render(request, "atelier/facts.html", {"page": page, "query": query, "institution": institution})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def documentary_search(request):
    from .models import InquiryCase
    from .services.document_index import search_passages
    from .services.research_cases import prepare_research_case
    if request.method=='POST':
        try:
            case,created=prepare_research_case(request.POST.get('q',''))
            return render(request,'atelier/research_case.html',{'case':case,'created':created})
        except ValidationError:
            return HttpResponse('Recherche invalide ; aucun dossier préparé.',status=400)
    query=request.GET.get('q','').strip()
    result,error=None,''
    if query:
        try:result=search_passages(query=query,cursor=request.GET.get('cursor',''))
        except ValidationError as exc:
            error='Le corpus a changé : relancez la recherche.' if 'corpus_changed' in str(exc) else 'Recherche ou curseur invalide. Relancez sans curseur.'
    return render(request,'atelier/documentary_search.html',{'query':query,'result':result,'error':error,'prepared_cases':InquiryCase.objects.order_by('-pk')[:50]})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def thematic_comparisons(request):
    """Explorateur local, non éditorial, des relations parole-mesure-vote."""
    from .services.thematic_comparisons import (
        compare_declaration,
        compare_measure,
        search_declarations,
        search_measures,
    )

    mode = request.GET.get("mode", "speech")
    if mode not in {"speech", "law"}:
        mode = "speech"
    query = request.GET.get("q", "").strip()
    institution = request.GET.get("institution", "").strip()
    date_from = request.GET.get("from", "").strip() or request.GET.get("period_start", "").strip()
    date_to = request.GET.get("to", "").strip() or request.GET.get("period_end", "").strip()
    conclusion = request.GET.get("conclusion", "").strip()
    legislature = request.GET.get("legislature", "").strip()
    nominative = request.GET.get("nominative", "").strip()
    try:
        offset = int(request.GET.get("offset", "0") or "0")
    except ValueError:
        offset = 0
    if "page" in request.GET and "offset" not in request.GET:
        try:
            page = max(1, int(request.GET.get("page") or 1))
        except ValueError:
            page = 1
        offset = (page - 1) * 20
    try:
        limit = int(request.GET.get("limit", "50") or "50")
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 50))
    result = None
    matches = []
    pagination = None
    search_meta = None
    error = ""
    compare_filters = {
        "institution": institution or None,
        "legislature": legislature or None,
        "nominative_status": nominative or None,
        "conclusion": conclusion or None,
        "period_start": date_from or None,
        "period_end": date_to or None,
    }
    try:
        if mode == "speech" and request.GET.get("declaration"):
            result = compare_declaration(
                declaration_id=int(request.GET["declaration"]),
                offset=offset,
                limit=limit,
                **compare_filters,
            )
            pagination = result["pagination"]
        elif mode == "law" and request.GET.get("measure"):
            result = compare_measure(
                measure_id=int(request.GET["measure"]),
                offset=offset,
                limit=limit,
                **compare_filters,
            )
            pagination = result["pagination"]
        elif query:
            search_meta = (
                search_declarations(
                    query=query,
                    offset=offset,
                    limit=min(limit, 20),
                    date_from=date_from or None,
                    date_to=date_to or None,
                )
                if mode == "speech"
                else search_measures(
                    query=query,
                    offset=offset,
                    limit=min(limit, 20),
                    institution=institution or None,
                    date_from=date_from or None,
                    date_to=date_to or None,
                )
            )
            matches = search_meta["items"]
            pagination = search_meta["pagination"]
    except (ValidationError, ValueError, Declaration.DoesNotExist, Measure.DoesNotExist):
        error = "Recherche invalide ou élément introuvable dans le corpus local."

    return render(
        request,
        "atelier/thematic_comparisons.html",
        {
            "mode": mode,
            "query": query,
            "institution": institution,
            "date_from": date_from,
            "date_to": date_to,
            "conclusion": conclusion,
            "legislature": legislature,
            "nominative": nominative,
            "limit": limit,
            "matches": matches,
            "result": result,
            "pagination": pagination,
            "search_meta": search_meta,
            "error": error,
            "filters": compare_filters,
            "declaration_count": Declaration.objects.count(),
            "measure_count": Measure.objects.count(),
            "speech_link_count": DeclarationMeasureLink.objects.count(),
            "vote_link_count": MeasureScrutinyLink.objects.count(),
            "can_qualify": request.user.dv_profile.role
            in {
                UserProfile.Role.ADMIN,
                UserProfile.Role.EDITOR,
                UserProfile.Role.REVIEWER,
                UserProfile.Role.OPERATOR,
            },
        },
    )


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def thematic_qualification(request):
    """Saisie humaine explicite des relations ; aucune revue éditoriale."""
    from .services.thematic_qualification import (
        record_declaration_measure_link,
        record_measure_scrutiny_link,
    )

    position_form = DeclarationMeasureQualificationForm(prefix="position")
    vote_form = MeasureScrutinyQualificationForm(prefix="vote")
    if request.method == "POST" and request.POST.get("kind") == "position":
        position_form = DeclarationMeasureQualificationForm(request.POST, prefix="position")
        if position_form.is_valid():
            record_declaration_measure_link(user=request.user, **position_form.cleaned_data)
            messages.success(request, "Relation parole → mesure enregistrée dans une nouvelle révision immuable.")
            return redirect("thematic_qualification")
    elif request.method == "POST" and request.POST.get("kind") == "vote":
        vote_form = MeasureScrutinyQualificationForm(request.POST, prefix="vote")
        if vote_form.is_valid():
            record_measure_scrutiny_link(user=request.user, **vote_form.cleaned_data)
            messages.success(request, "Relation mesure → scrutin enregistrée dans une nouvelle révision immuable.")
            return redirect("thematic_qualification")

    return render(
        request,
        "atelier/thematic_qualification.html",
        {
            "position_form": position_form,
            "vote_form": vote_form,
            "position_history": DeclarationMeasureLink.objects.select_related(
                "declaration", "measure", "recorded_by"
            ).order_by("-created_at")[:20],
            "vote_history": MeasureScrutinyLink.objects.select_related(
                "measure", "scrutiny", "recorded_by"
            ).order_by("-created_at")[:20],
        },
    )


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def research_preparation(request, pk):
    from .models import InquiryCase
    case=get_object_or_404(InquiryCase,pk=pk)
    return render(request,'atelier/research_case.html',{'case':case,'created':False})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR, UserProfile.Role.COLLECTOR)
def staged_votes(request):
    from .models import StagedScrutiny,ArchiveScan
    query=request.GET.get('q','').strip()[:500]
    rows=StagedScrutiny.objects.select_related('document__source').order_by('source_uid')
    if query:rows=rows.filter(Q(source_uid__icontains=query)|Q(normalized__official_title__icontains=query))
    return render(request,'atelier/staged_votes.html',{'query':query,'page':Paginator(rows,25).get_page(request.GET.get('page')),'scans':ArchiveScan.objects.select_related('document__source')})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def dossiers(request):
    query = request.GET.get("q", "").strip()
    state = request.GET.get("state", "").strip()
    rows = EditorialDossier.objects.order_by("-updated_at")
    if query:
        rows = rows.filter(stable_id__icontains=query)
    if state:
        rows = rows.filter(state=state)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    environment = _runtime_environment()
    for dossier in page.object_list:
        dossier.current_item = dossier.revisions.filter(revision=dossier.current_revision).first()
        dossier.eligibility = (
            evaluate_revision(dossier.current_item, environment)
            if dossier.current_item else None
        )
    return render(request, "atelier/dossiers.html", {"page": page, "query": query, "state": state})


@login_required
@permission_required("editorial.review.prepare")
def review(request, pk):
    dossier = get_object_or_404(EditorialDossier, pk=pk)
    revision = get_object_or_404(dossier.revisions, revision=dossier.current_revision)
    previous = dossier.revisions.filter(revision__lt=revision.revision).order_by("-revision").first()
    before = json.dumps(previous.public_payload, ensure_ascii=False, indent=2, sort_keys=True).splitlines() if previous else []
    after = json.dumps(revision.public_payload, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    differences = list(difflib.unified_diff(before, after, fromfile="révision précédente", tofile="révision courante", lineterm=""))
    request.session["last_reviewed_dossier"] = dossier.pk
    action_form = ReviewActionForm(initial={"expected_revision": revision.revision, "expected_digest": revision.snapshot_digest})
    correction_form = CorrectionForm(
        initial={
            "expected_revision": revision.revision,
            "expected_digest": revision.snapshot_digest,
            "title": revision.title,
            "short_answer": revision.short_answer,
            "main_caveat": revision.main_caveat,
            "reasoning": revision.reasoning,
        }
    )
    from .models import ResearchPreparation

    preparation = None
    if revision.comparison_id:
        preparation = (
            ResearchPreparation.objects.filter(payload__comparison_id=revision.comparison.stable_id)
            .order_by("-created_at", "-pk")
            .first()
        )
    from .services.diffusion import preview as diffusion_preview
    return render(
        request,
        "atelier/review.html",
        {
            "dossier": dossier,
            "revision": revision,
            "action_form": action_form,
            "correction_form": correction_form,
            "differences": differences,
            "public_snapshot_json": "\n".join(after),
            "diffusion_preview": diffusion_preview(revision),
            "controls": revision.controls.filter(environment=_runtime_environment()).order_by("control_type"),
            "preparation": preparation,
        },
    )


@login_required
def detached_review(request, pk):
    from .services.full_review import build_review_bundle
    from .services.detached_review import accept_detached_review
    from .services.review_authority import may_access_detached_review_surface

    try:
        profile = request.user.dv_profile
    except UserProfile.DoesNotExist:
        return HttpResponseForbidden("Profil absent.")
    if not may_access_detached_review_surface(profile, _runtime_environment()):
        return HttpResponseForbidden("Ce compte ne peut pas accéder à la signature fictive.")
    dossier=get_object_or_404(EditorialDossier,pk=pk)
    if _runtime_environment()!='test' or dossier.data_scope!='synthetic' or not request.user.dv_profile.is_test_identity:
        return HttpResponseForbidden('Parcours de signature fictive seulement ; identité réelle non activée.')
    revision=get_object_or_404(dossier.revisions,revision=dossier.current_revision)
    if request.method=='POST':
        uploaded=request.FILES.get('signed_review')
        if uploaded is None or uploaded.size>2*1024*1024:
            return HttpResponse('Fichier de retour absent ou trop volumineux.',status=400)
        try:
            value=json.loads(uploaded.read())
            if not isinstance(value,dict) or set(value)!={'bundle','proof'}:raise ValueError()
            review=accept_detached_review(dossier_id=pk,expected_revision=revision.revision,bundle=value['bundle'],proof=value['proof'],
                                         user=request.user,environment='test',destination='dv-site:test')
            messages.success(request,'Signature fictive vérifiée et audit conservé. Aucun envoi effectué.')
            return redirect('review',pk=review.revision.dossier_id)
        except (ValidationError,ValueError,KeyError,TypeError):
            return HttpResponse('Retour refusé : signature, autorité ou version non conforme.',status=400)
    if request.method!='GET':return HttpResponse(status=405)
    if request.GET.get('download')=='bundle':
        try: bundle=build_review_bundle(revision,environment='test',destination='dv-site:test')
        except ValidationError:return HttpResponse('Révision non disponible pour signature.',status=409)
        response=JsonResponse(bundle,json_dumps_params={'ensure_ascii':False})
        response['Content-Disposition']='attachment; filename="review-test-bundle.json"'
        return response
    return render(request,'atelier/detached_review.html',{'dossier':dossier,'revision':revision})


@login_required
@permission_required("editorial.review.prepare")
@require_POST
def review_action(request, pk):
    dossier = get_object_or_404(EditorialDossier, pk=pk)
    mode = request.POST.get("mode", "review")
    environment = _runtime_environment()
    try:
        if mode == "correct":
            form = CorrectionForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Correction refusée : formulaire incomplet.")
                return redirect("review", pk=pk)
            current = get_object_or_404(dossier.revisions, revision=form.cleaned_data["expected_revision"])
            if current.snapshot_digest != form.cleaned_data["expected_digest"] or dossier.current_revision != current.revision:
                return HttpResponse("Conflit : cette révision n'est plus courante.", status=409)
            record_human_review(
                dossier_id=dossier.pk,
                expected_revision=current.revision,
                expected_digest=current.snapshot_digest,
                user=request.user,
                decision="request_changes",
                environment=environment,
                note=form.cleaned_data["change_summary"],
            )
            payload = dict(current.public_payload)
            payload.update(
                title=form.cleaned_data["title"],
                short_answer=form.cleaned_data["short_answer"],
                main_caveat=form.cleaned_data["main_caveat"],
                reasoning=form.cleaned_data["reasoning"],
            )
            new_revision = create_revision(
                dossier=dossier,
                public_payload=payload,
                source_snapshot=current.source_snapshot,
                title=form.cleaned_data["title"],
                short_answer=form.cleaned_data["short_answer"],
                main_caveat=form.cleaned_data["main_caveat"],
                reasoning=form.cleaned_data["reasoning"],
                result=current.result,
                method_version=current.method_version,
                change_summary=form.cleaned_data["change_summary"],
                created_by=request.user,
                comparison=current.comparison,
            )
            record_controls(new_revision, deterministic_control_results(new_revision), environment=environment)
            messages.success(request, f"Révision {new_revision.revision} créée ; elle doit être présentée puis revue.")
        else:
            form = ReviewActionForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Action refusée : données de révision invalides.")
                return redirect("review", pk=pk)
            review_event = record_human_review(
                dossier_id=dossier.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                expected_digest=form.cleaned_data["expected_digest"],
                user=request.user,
                decision=form.cleaned_data["decision"],
                environment=environment,
                note=form.cleaned_data["note"],
            )
            if review_event.decision == "approve":
                event = authorize_and_queue(review_event.revision, environment)
                messages.success(request, f"Version revue et placée dans la boîte d'envoi locale (événement {event.sequence}).")
            else:
                messages.success(request, "Décision enregistrée sur cette révision.")
    except PermissionDenied:
        return HttpResponseForbidden("Ce compte ne peut pas enregistrer cette revue.")
    except ValidationError as exc:
        code = exc.messages[0] if exc.messages else "validation_error"
        status = 409 if "conflict" in code else 422
        return HttpResponse(f"Action refusée : {code}", status=status)
    return redirect("review", pk=pk)


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def publications(request):
    page = Paginator(OutboxEvent.objects.select_related("dossier").order_by("-created_at"), 25).get_page(request.GET.get("page"))
    return render(request, "atelier/publications.html", {"page": page})


def health(_request):
    from django.db import connection
    from .models import FeedCursor, JobRun, RelationCandidate
    from .services.storage_lifecycle import storage_metrics

    database = "ok"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database = "error"
    last_job = (
        JobRun.objects.filter(task_name__in={"private_source_collection", "permanent_feed_collection"})
        .order_by("-updated_at")
        .first()
    )
    collection = "never_run" if last_job is None else last_job.state
    restored_unreconciled = (settings.RUNTIME_DIR / ".dv-restored-unreconciled").exists()
    feeds = list(
        FeedCursor.objects.values("feed_id", "last_success_at", "block_reason", "ingestion_active", "family")
    )
    freshness = {
        row["feed_id"]: {
            "last_success_at": row["last_success_at"].isoformat() if row["last_success_at"] else None,
            "block_reason": row["block_reason"] or None,
            "ingestion_active": row["ingestion_active"],
            "family": row["family"],
        }
        for row in feeds
    }
    payload = {
        "status": "ok" if database == "ok" else "degraded",
        "application": "ok",
        "database": database,
        "last_collection": collection,
        "last_collection_error": "" if last_job is None else last_job.error_code,
        "restored_unreconciled": restored_unreconciled,
        "scope": "local_or_private",
        "publication": "not_authorized",
        "feeds_freshness": freshness,
        "relation_candidates_proposed": RelationCandidate.objects.filter(status="proposed").count(),
        "storage": storage_metrics(),
        "no_political_performance_metrics": True,
    }
    return JsonResponse(payload, status=200 if database == "ok" else 503)


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def explain_index(request):
    from .services.explain import latest_entries

    return render(request, "atelier/explain_index.html", {"entries": latest_entries()})


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def explain_detail(request, stable_id: str):
    from .models import ExplainEntry
    from .services.explain import entry_payload

    entry = (
        ExplainEntry.objects.filter(stable_id=stable_id, active=True).order_by("-version").first()
    )
    if entry is None:
        return render(request, "atelier/error.html", {"message": "Fiche introuvable"}, status=404)
    return render(
        request,
        "atelier/explain_detail.html",
        {"entry": entry, "payload": entry_payload(entry)},
    )


def explain_api(_request):
    from .services.explain import entry_payload, latest_entries, registry_extension_point

    return JsonResponse(
        {
            "entries": [entry_payload(e) for e in latest_entries()],
            "extension_point": registry_extension_point(),
            "partisan_commentary": False,
        }
    )


@login_required
@roles_required(UserProfile.Role.REVIEWER, UserProfile.Role.OPERATOR)
def feeds_coverage(request):
    from .services.feeds import coverage_matrix

    return render(request, "atelier/feeds_coverage.html", {"rows": coverage_matrix()})


def feeds_coverage_api(_request):
    from .services.feeds import coverage_matrix

    return JsonResponse({"feeds": coverage_matrix(), "no_political_scores": True})


def csrf_failure(request, reason=""):
    return render(request, "atelier/error.html", {"message": "Requête refusée (protection CSRF)."}, status=403)


@login_required
def password_change_required(request):
    from atelier.forms import MandatoryPasswordChangeForm
    from atelier.services.accounts import complete_password_change

    try:
        profile = request.user.dv_profile
    except UserProfile.DoesNotExist:
        return HttpResponseForbidden("Profil absent")
    if not profile.must_change_password:
        return redirect("dashboard")
    form = MandatoryPasswordChangeForm(user=request.user, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        complete_password_change(
            user=request.user,
            new_password=form.cleaned_data["new_password1"],
            request=request,
        )
        messages.success(request, "Mot de passe mis à jour.")
        return redirect("dashboard")
    return render(request, "registration/password_change_required.html", {"form": form})


def invite_redeem(request):
    from atelier.forms import InviteRedeemForm
    from atelier.services.accounts import redeem_invite_token
    from django.contrib.auth import login

    form = InviteRedeemForm(data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = redeem_invite_token(
                raw_token=form.cleaned_data["token"],
                new_password=form.cleaned_data["new_password1"],
            )
        except ValidationError as exc:
            messages.error(request, "Invitation refusée.")
            form.add_error(None, exc.message if hasattr(exc, "message") else str(exc))
        else:
            login(request, user)
            from atelier.services.accounts import bind_session_epoch

            bind_session_epoch(request, user)
            messages.success(request, "Compte activé.")
            return redirect("dashboard")
    return render(request, "registration/invite_redeem.html", {"form": form})


@login_required
@permission_required("accounts.manage")
def accounts_list(request):
    rows = (
        UserProfile.objects.select_related("user")
        .order_by("login_pseudonym", "user__username")
    )
    return render(request, "atelier/accounts_list.html", {"profiles": rows})


@login_required
@permission_required("accounts.manage")
def accounts_create(request):
    from atelier.forms import AccountCreateForm
    from atelier.services.accounts import create_account

    form = AccountCreateForm(data=request.POST or None)
    created = None
    one_shot_secret = None
    if request.method == "POST" and form.is_valid():
        if not form.cleaned_data.get("confirm_create"):
            form.add_error("confirm_create", "Confirmez la création pour continuer.")
        else:
            created = create_account(
                role=form.cleaned_data["role"],
                actor=request.user,
                is_test_identity=form.cleaned_data.get("is_test_identity", False),
                use_invite_token=form.cleaned_data["access_mode"] == "invite",
            )
            # Secret initial affiché une seule fois dans la réponse — jamais stocké en session/DB.
            one_shot_secret = {
                "login": created.login_pseudonym,
                "temporary_password": created.temporary_password,
                "invite_token": created.invite_token,
                "role": created.role,
                "internal_uid": created.internal_uid,
            }
            messages.success(
                request,
                "Compte créé. Remettez le secret initial hors application (une seule fois).",
            )
            form = AccountCreateForm()  # formulaire vierge après succès
    return render(
        request,
        "atelier/accounts_create.html",
        {"form": form, "created": created, "one_shot_secret": one_shot_secret},
    )


@login_required
@permission_required("accounts.manage")
@require_POST
def accounts_action(request, pk: int):
    from atelier.services.accounts import (
        change_role,
        regenerate_login_pseudonym,
        reset_access,
        set_account_status,
    )

    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=pk)
    target = profile.user
    action = request.POST.get("action", "").strip()
    confirm = request.POST.get("confirm", "") == "1"
    irreversible = action in {"revoke", "reset", "regenerate_pseudonym"}
    if irreversible and not confirm:
        messages.error(request, "Confirmation requise pour une action irréversible.")
        return redirect("accounts_list")
    one_shot_secret = None
    try:
        if action == "suspend":
            set_account_status(
                target_user=target,
                actor=request.user,
                status=UserProfile.AccountStatus.SUSPENDED,
                reason="admin_ui",
            )
            messages.success(request, "Compte suspendu.")
        elif action == "reactivate":
            set_account_status(
                target_user=target,
                actor=request.user,
                status=UserProfile.AccountStatus.ACTIVE,
                reason="admin_ui",
            )
            messages.success(request, "Compte réactivé.")
        elif action == "revoke":
            set_account_status(
                target_user=target,
                actor=request.user,
                status=UserProfile.AccountStatus.REVOKED,
                reason="admin_ui",
            )
            messages.success(request, "Compte révoqué.")
        elif action == "reset":
            creds = reset_access(
                target_user=target,
                actor=request.user,
                use_invite_token=request.POST.get("access_mode") == "invite",
            )
            one_shot_secret = {
                "login": creds.login_pseudonym,
                "temporary_password": creds.temporary_password,
                "invite_token": creds.invite_token,
            }
            messages.success(request, "Accès réinitialisé (sessions révoquées). Secret affiché une seule fois.")
            rows = UserProfile.objects.select_related("user").order_by("login_pseudonym", "user__username")
            return render(
                request,
                "atelier/accounts_list.html",
                {"profiles": rows, "one_shot_secret": one_shot_secret},
            )
        elif action == "regenerate_pseudonym":
            regenerate_login_pseudonym(target_user=target, actor=request.user)
            messages.success(request, "Pseudonyme de connexion régénéré.")
        elif action == "set_role":
            change_role(target_user=target, actor=request.user, new_role=request.POST.get("role", ""))
            messages.success(request, "Rôle mis à jour.")
        else:
            messages.error(request, "Action inconnue.")
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
    return redirect("accounts_list")

