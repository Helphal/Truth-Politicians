from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import resolve, Resolver404


class SecurityHeadersMiddleware:
    """En-têtes défensifs sans chargement de ressources externes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        return response


class AccountSessionMiddleware:
    """Révoque les sessions périmées et force le changement de mot de passe initial."""

    ALLOWED_WHEN_PASSWORD_REQUIRED = frozenset(
        {
            "password_change_required",
            "logout",
            "login",
            "invite_redeem",
        }
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        from atelier.models import UserProfile
        from atelier.services.accounts import session_epoch_valid, touch_activity

        try:
            profile = user.dv_profile
        except UserProfile.DoesNotExist:
            return None
        if not session_epoch_valid(request, user):
            logout(request)
            return redirect("login")
        if profile.is_account_usable():
            touch_activity(user)
        url_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
        if url_name is None:
            try:
                url_name = resolve(request.path_info).url_name
            except Resolver404:
                url_name = None
        if (
            profile.is_account_usable()
            and profile.must_change_password
            and url_name not in self.ALLOWED_WHEN_PASSWORD_REQUIRED
        ):
            return redirect("password_change_required")
        return None
