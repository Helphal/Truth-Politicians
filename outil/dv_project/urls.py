from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from atelier.views import SecureLoginView, invite_redeem, password_change_required

admin.site.site_header = "Truth Politicians — administration technique"
admin.site.site_title = "Truth Politicians"
admin.site.index_title = "Administration de l'outil"


urlpatterns = [
    path("connexion/", SecureLoginView.as_view(), name="login"),
    path("deconnexion/", auth_views.LogoutView.as_view(), name="logout"),
    path("mot-de-passe/obligatoire/", password_change_required, name="password_change_required"),
    path("invitation/", invite_redeem, name="invite_redeem"),
    path("administration-technique/", admin.site.urls),
    path("", include("atelier.urls")),
]
