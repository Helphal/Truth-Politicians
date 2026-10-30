"""Read-only readiness assessment; no secret values, deployment or network probes."""
from django.conf import settings

def assess_private_readiness():
    from atelier.models import UserProfile
    checks={
        'private_environment':settings.DV_ENV=='private',
        'debug_disabled':not settings.DEBUG,
        'hosts_explicit_and_no_wildcard':bool(settings.ALLOWED_HOSTS) and '*' not in settings.ALLOWED_HOSTS and settings.DV_ENV=='private',
        'tls_redirect':getattr(settings,'SECURE_SSL_REDIRECT',False),
        'secure_session_cookie':settings.SESSION_COOKIE_SECURE,
        'secure_csrf_cookie':settings.CSRF_COOKIE_SECURE,
        'network_not_direct':settings.DV_NETWORK_MODE in {'disabled','protected'},
        'actual_reviewer_configured':UserProfile.objects.filter(role='reviewer',active=True,is_test_identity=False,user__is_active=True).exists(),
        'restored_state_reconciled':not (settings.RUNTIME_DIR/'.dv-restored-unreconciled').exists(),
    }
    return {'scope':'read_only_configuration_preflight','checks':checks,'configuration_ready':all(checks.values()),
        'live_tls_tested':False,'nas_target_authority_verified':False,'installed_privately':False,'publication_authorized':False,
        'notes':['Secrets validated by settings at private startup; values never reported.',
                 'A local test result does not establish private TLS, a NAS destination or deployment authority.']}
