from django.conf import settings


def environment(_request):
    return {
        "dv_environment": settings.DV_ENV,
        "dv_schema": settings.DV_PUBLICATION_SCHEMA,
    }


def account_nav(request):
    is_admin = False
    attribution = ""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        try:
            profile = user.dv_profile
            is_admin = profile.role == "admin" and profile.is_account_usable()
            attribution = profile.public_attribution_label or profile.login_pseudonym
        except Exception:
            is_admin = False
    return {
        "dv_is_account_admin": is_admin,
        "dv_attribution_label": attribution,
    }
