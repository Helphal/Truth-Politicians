from django.contrib import admin

from .models import (
    CorrectionRequest,
    Coverage,
    EditorialDossier,
    JobRun,
    LegislativeDossier,
    Organization,
    Person,
    Scrutiny,
    Source,
    TextVersion,
    Theme,
    RemoteExposure,
)


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("stable_id", "producer", "family", "access_status", "rights_status", "last_success_at")
    list_filter = ("family", "access_status", "rights_status")
    search_fields = ("stable_id", "producer")


@admin.register(Scrutiny)
class ScrutinyAdmin(admin.ModelAdmin):
    list_display = ("stable_id", "institution", "occurred_on", "result_status", "nominative_status")
    list_filter = ("institution", "result_status", "nominative_status")
    search_fields = ("stable_id", "official_title")


@admin.register(EditorialDossier)
class EditorialDossierAdmin(admin.ModelAdmin):
    list_display = ("stable_id", "content_kind", "data_scope", "state", "current_revision", "suspended")
    readonly_fields = ("established_kind", "current_revision")
    search_fields = ("stable_id",)


admin.site.register([Person, Organization, LegislativeDossier, TextVersion, Theme, Coverage, JobRun, CorrectionRequest])


@admin.register(RemoteExposure)
class RemoteExposureAdmin(admin.ModelAdmin):
    list_display = ("event", "withdrawal_requested", "suspension", "resolved_at")
    list_filter = ("withdrawal_requested", "event__environment")
    readonly_fields = ("event", "withdrawal_requested", "suspension", "resolved_at", "verified_receipt", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

# HumanReview, PublicationSnapshot et OutboxEvent ne sont volontairement pas
# modifiables dans l'administration : leurs écritures passent par les services
# transactionnels et, pour la revue, par l'action humaine protégée par CSRF.
