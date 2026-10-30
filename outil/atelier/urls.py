from django.urls import path

from . import views
from . import verification_views


urlpatterns = [
    path('verification/', verification_views.index, name='verification_index'),
    path('verification/<int:pk>/', verification_views.detail, name='verification_detail'),
    path('revision/<int:pk>/verifier/', verification_views.request_verification, name='verification_request'),
    path("", views.dashboard, name="dashboard"),
    path("sources/", views.sources, name="sources"),
    path("recherche-documentaire/", views.documentary_search, name="documentary_search"),
    path("parole-et-votes/", views.thematic_comparisons, name="thematic_comparisons"),
    path("parole-et-votes/qualifier/", views.thematic_qualification, name="thematic_qualification"),
    path("recherches/<int:pk>/", views.research_preparation, name="research_preparation"),
    path("archives-indexees/", views.staged_votes, name="staged_votes"),
    path("textes-et-votes/", views.facts, name="facts"),
    path("dossiers/", views.dossiers, name="dossiers"),
    path("dossiers/<int:pk>/", views.review, name="review"),
    path("dossiers/<int:pk>/action/", views.review_action, name="review_action"),
    path("dossiers/<int:pk>/signature-test/", views.detached_review, name="detached_review"),
    path("publications/", views.publications, name="publications"),
    path("sante/", views.health, name="health"),
    path("comprendre/", views.explain_index, name="explain_index"),
    path("comprendre/<path:stable_id>/", views.explain_detail, name="explain_detail"),
    path("alimentation/", views.feeds_coverage, name="feeds_coverage"),
    path("comptes/", views.accounts_list, name="accounts_list"),
    path("comptes/creer/", views.accounts_create, name="accounts_create"),
    path("comptes/<int:pk>/action/", views.accounts_action, name="accounts_action"),
    path("api/explain/", views.explain_api, name="explain_api"),
    path("api/feeds/coverage/", views.feeds_coverage_api, name="feeds_coverage_api"),
]
