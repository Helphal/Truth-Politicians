"""Configuration de l'atelier privé Truth Politicians.

La configuration locale est sûre par défaut : écoute sur loopback, base dans
``runtime/`` et aucune tentative de récupérer un secret sur le réseau. Une
configuration déclarée ``production`` échoue immédiatement si les valeurs
obligatoires ne sont pas fournies.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("DV_RUNTIME_DIR", BASE_DIR / "runtime")).resolve()
# Private, bound historical review pieces are opt-in and never change gates.
# Keep the supplied path unresolved so the reader can reject links/junctions.
DV_VERIFICATION_REVIEW_ARTIFACT_ROOT = (
    Path(os.environ["DV_VERIFICATION_REVIEW_ARTIFACT_ROOT"])
    if os.environ.get("DV_VERIFICATION_REVIEW_ARTIFACT_ROOT") else None
)
DV_ENV = os.environ.get("DV_ENV", "local").strip().lower()
if DV_ENV not in {"local", "test", "private", "production"}:
    raise ImproperlyConfigured("DV_ENV doit valoir local, test, private ou production")

_secret = os.environ.get("DV_SECRET_KEY", "")
if DV_ENV in {"private", "production"} and len(_secret) < 50:
    raise ImproperlyConfigured("DV_SECRET_KEY absent ou trop court pour cet environnement")
SECRET_KEY = _secret or "dv-local-test-key-explicitly-not-for-private-or-production-use"

DEBUG = DV_ENV == "local" and os.environ.get("DV_DEBUG", "0") == "1"
_hosts = [value.strip() for value in os.environ.get("DV_ALLOWED_HOSTS", "").split(",") if value.strip()]
if DV_ENV in {"private", "production"} and not _hosts:
    raise ImproperlyConfigured("DV_ALLOWED_HOSTS est obligatoire hors environnement local/test")
ALLOWED_HOSTS = _hosts or ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "atelier.apps.AtelierConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "atelier.middleware.AccountSessionMiddleware",
    "atelier.services.private_admin_entry.PrivateAdminEntryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "atelier.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "dv_project.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "atelier.context_processors.environment",
                "atelier.context_processors.account_nav",
            ],
        },
    },
]

WSGI_APPLICATION = "dv_project.wsgi.application"
ASGI_APPLICATION = "dv_project.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DV_DATABASE_PATH", str(RUNTIME_DIR / "atelier.sqlite3")),
        # One SQLite writer: reserve it at entry to every outer atomic block.
        # A contending writer waits at most 20 s then fails; no partial retry.
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 14}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "atelier.password_validation.StrongPasswordComplexityValidator"},
]

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = RUNTIME_DIR / "static"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Les preuves externes complètes restent dans le volume privé du runtime. Le
# répertoire statique servi par Caddy et la zone d'export filtrée sont
# volontairement distincts du coffre.
DV_EVIDENCE_VAULT_PATH = Path(
    os.environ.get("DV_EVIDENCE_VAULT_PATH", RUNTIME_DIR / "evidence-vault")
).resolve()
DV_RIGHTS_EVIDENCE_DIR = Path(
    os.environ.get("DV_RIGHTS_EVIDENCE_DIR", RUNTIME_DIR.parent / "droits" / "conditions")
)
DV_VERIFICATION_PLAN_ROOT = Path(
    os.environ.get("DV_VERIFICATION_PLAN_ROOT", RUNTIME_DIR / "verification-plans")
)
DV_PUBLIC_EVIDENCE_STAGING_PATH = Path(
    os.environ.get("DV_PUBLIC_EVIDENCE_STAGING_PATH", RUNTIME_DIR / "public-evidence-staging")
).resolve()
DV_EVIDENCE_VAULT_MAX_BYTES = int(os.environ.get("DV_EVIDENCE_VAULT_MAX_BYTES", str(50 * 1024**3)))

if RUNTIME_DIR != DV_EVIDENCE_VAULT_PATH and RUNTIME_DIR not in DV_EVIDENCE_VAULT_PATH.parents:
    raise ImproperlyConfigured("Le coffre de preuves doit rester dans le volume runtime privé")
if DV_EVIDENCE_VAULT_PATH == STATIC_ROOT or DV_EVIDENCE_VAULT_PATH in STATIC_ROOT.parents or STATIC_ROOT in DV_EVIDENCE_VAULT_PATH.parents:
    raise ImproperlyConfigured("Le coffre de preuves ne peut pas chevaucher les fichiers statiques")
if DV_PUBLIC_EVIDENCE_STAGING_PATH == DV_EVIDENCE_VAULT_PATH or DV_PUBLIC_EVIDENCE_STAGING_PATH in DV_EVIDENCE_VAULT_PATH.parents or DV_EVIDENCE_VAULT_PATH in DV_PUBLIC_EVIDENCE_STAGING_PATH.parents:
    raise ImproperlyConfigured("Le sas public ne peut pas chevaucher le coffre privé")
if not 1024**2 <= DV_EVIDENCE_VAULT_MAX_BYTES <= 10 * 1024**4:
    raise ImproperlyConfigured("DV_EVIDENCE_VAULT_MAX_BYTES hors bornes")

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_NAME = "dv_atelier_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = DV_ENV in {"private", "production"}
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_NAME = "dv_atelier_csrf"
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = DV_ENV in {"private", "production"}
CSRF_FAILURE_VIEW = "atelier.views.csrf_failure"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

_trust_proxy_ssl_header = os.environ.get("DV_TRUST_PROXY_SSL_HEADER", "0")
if _trust_proxy_ssl_header not in {"0", "1"}:
    raise ImproperlyConfigured("DV_TRUST_PROXY_SSL_HEADER doit valoir 0 ou 1")
if _trust_proxy_ssl_header == "1":
    # À activer seulement si le reverse proxy de confiance remplace cet en-tête.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if DV_ENV in {"private", "production"}:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = DV_ENV == "production"
    SECURE_HSTS_PRELOAD = DV_ENV == "production"

_csrf_trusted = os.environ.get("DV_CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [value.strip() for value in _csrf_trusted.split(",") if value.strip()]

DV_PUBLICATION_SCHEMA = "dv.content/0.4.0-racc001-draft.1"
DV_POLICY_VERSION = "DV-PUB-RACC1"
DV_EVENT_SCHEMA = "dv.event/0.2.0-racc001-draft.1"
DV_EVENT_KEY_ID = os.environ.get("DV_EVENT_KEY_ID", "test-v1" if DV_ENV in {"local", "test"} else "")
DV_EVENT_KEYS = {}  # Clés par cible à configurer hors dépôt ; aucune clé privée implicite.
DV_RECEIPT_KEY_ID = os.environ.get(
    "DV_RECEIPT_KEY_ID",
    "test-v1" if DV_ENV in {"local", "test"} else "",
).strip()
DV_EVENT_TTL_SECONDS = 300
DV_PUBLICATION_SEND_AUTHORIZED = False
DV_RECEIVER_STATE_RECONCILED = False
DV_CANONICALIZATION = "DV-CANON-JSON-V1"
DV_REVIEW_ATTESTATION_KEY = os.environ.get("DV_REVIEW_ATTESTATION_KEY", "")
if DV_ENV in {"private", "production"} and len(DV_REVIEW_ATTESTATION_KEY) < 32:
    raise ImproperlyConfigured("DV_REVIEW_ATTESTATION_KEY est obligatoire hors local/test")

# Titulaire unique de la revue finale (internal_uid immuable). Vide = refus par défaut hors TEST explicite.
DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID = os.environ.get("DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID", "").strip()

DV_SOURCE_ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.environ.get(
        "DV_SOURCE_ALLOWED_HOSTS",
        ",".join(
            [
                "data.assemblee-nationale.fr",
                "www.assemblee-nationale.fr",
                "data.senat.fr",
                "www.senat.fr",
                "www.elysee.fr",
                "www.gouvernement.fr",
                "www.legifrance.gouv.fr",
                "api.piste.gouv.fr",
                "www.data.gouv.fr",
                "public.api.bsky.app",
                "www.youtube.com",
            ]
        ),
    ).split(",")
    if host.strip()
}
# Cibles configurables pour connecteurs paroles (listes séparées par virgules).
DV_FEED_RSS_URLS = [u.strip() for u in os.environ.get("DV_FEED_RSS_URLS", "").split(",") if u.strip()]
DV_FEED_PODCAST_URLS = [u.strip() for u in os.environ.get("DV_FEED_PODCAST_URLS", "").split(",") if u.strip()]
DV_FEED_YOUTUBE_CHANNELS = [
    u.strip() for u in os.environ.get("DV_FEED_YOUTUBE_CHANNELS", "").split(",") if u.strip()
]
DV_FEED_BLUESKY_HANDLES = [
    u.strip() for u in os.environ.get("DV_FEED_BLUESKY_HANDLES", "").split(",") if u.strip()
]
# Seuils STORAGE-01 (octets) — configurables, pas magiques dans le code métier.
DV_BLOB_LIGHT_MAX_BYTES = int(os.environ.get("DV_BLOB_LIGHT_MAX_BYTES", str(256 * 1024)))
DV_BLOB_MEDIUM_MAX_BYTES = int(os.environ.get("DV_BLOB_MEDIUM_MAX_BYTES", str(5 * 1024 * 1024)))
DV_BLOB_QUARANTINE_DAYS = int(os.environ.get("DV_BLOB_QUARANTINE_DAYS", "7"))
if not 1024 <= DV_BLOB_LIGHT_MAX_BYTES <= DV_BLOB_MEDIUM_MAX_BYTES:
    raise ImproperlyConfigured("DV_BLOB_LIGHT_MAX_BYTES incohérent")
if DV_BLOB_MEDIUM_MAX_BYTES > 500 * 1024 * 1024:
    raise ImproperlyConfigured("DV_BLOB_MEDIUM_MAX_BYTES hors borne raisonnable")
DV_FEED_CADENCE_SECONDS = {
    "fast": int(os.environ.get("DV_FEED_FAST_SECONDS", str(20 * 60))),
    "hourly": int(os.environ.get("DV_FEED_HOURLY_SECONDS", str(60 * 60))),
    "daily": int(os.environ.get("DV_FEED_DAILY_SECONDS", str(24 * 60 * 60))),
    "nightly": int(os.environ.get("DV_FEED_NIGHTLY_SECONDS", str(24 * 60 * 60))),
}
# BUILD-002-R4 : budgets drain ingest + détails discovery→détail
DV_FEED_INGEST_BUDGET_OBJECTS = int(os.environ.get("DV_FEED_INGEST_BUDGET_OBJECTS", "5000"))
DV_FEED_INGEST_BUDGET_SECONDS = float(os.environ.get("DV_FEED_INGEST_BUDGET_SECONDS", "120"))
DV_FEED_INGEST_MAX_CHUNKS = int(os.environ.get("DV_FEED_INGEST_MAX_CHUNKS", "50"))
DV_FEED_DETAIL_MAX_PER_RUN = int(os.environ.get("DV_FEED_DETAIL_MAX_PER_RUN", "10"))
DV_FEED_DETAIL_MAX_BYTES = int(os.environ.get("DV_FEED_DETAIL_MAX_BYTES", str(2 * 1024 * 1024)))
if not 1 <= DV_FEED_INGEST_BUDGET_OBJECTS <= 500_000:
    raise ImproperlyConfigured("DV_FEED_INGEST_BUDGET_OBJECTS hors bornes")
if not 1 <= DV_FEED_INGEST_BUDGET_SECONDS <= 3600:
    raise ImproperlyConfigured("DV_FEED_INGEST_BUDGET_SECONDS hors bornes")
if not 1 <= DV_FEED_INGEST_MAX_CHUNKS <= 10_000:
    raise ImproperlyConfigured("DV_FEED_INGEST_MAX_CHUNKS hors bornes")
if not 0 <= DV_FEED_DETAIL_MAX_PER_RUN <= 500:
    raise ImproperlyConfigured("DV_FEED_DETAIL_MAX_PER_RUN hors bornes")
if not 1024 <= DV_FEED_DETAIL_MAX_BYTES <= 20 * 1024 * 1024:
    raise ImproperlyConfigured("DV_FEED_DETAIL_MAX_BYTES hors bornes")
DV_NETWORK_MODE = os.environ.get("DV_NETWORK_MODE", "disabled").strip().lower()
if DV_NETWORK_MODE not in {"disabled", "direct", "protected"}:
    raise ImproperlyConfigured("DV_NETWORK_MODE doit valoir disabled, direct ou protected")
DV_PROTECTED_EGRESS_CONFIRMED = os.environ.get("DV_PROTECTED_EGRESS_CONFIRMED", "0") == "1"
if DV_NETWORK_MODE == "protected" and not DV_PROTECTED_EGRESS_CONFIRMED:
    raise ImproperlyConfigured("Sortie protégée demandée mais non confirmée : arrêt sans repli direct")
DV_PROTECTED_TRANSPORT_ENDPOINT = os.environ.get("DV_PROTECTED_TRANSPORT_ENDPOINT", "").strip()
DV_PROTECTED_EGRESS_PROFILE = os.environ.get("DV_PROTECTED_EGRESS_PROFILE", "TOR").strip().upper()
DV_PROTECTED_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("DV_PROTECTED_REQUEST_TIMEOUT_SECONDS", "30"))
DV_PROTECTED_POLL_SECONDS = float(os.environ.get("DV_PROTECTED_POLL_SECONDS", "1"))
if not 2 <= DV_PROTECTED_REQUEST_TIMEOUT_SECONDS <= 180:
    raise ImproperlyConfigured("DV_PROTECTED_REQUEST_TIMEOUT_SECONDS hors bornes")
if not 0.1 <= DV_PROTECTED_POLL_SECONDS <= 10:
    raise ImproperlyConfigured("DV_PROTECTED_POLL_SECONDS hors bornes")
if DV_NETWORK_MODE == "protected" and not DV_PROTECTED_TRANSPORT_ENDPOINT:
    raise ImproperlyConfigured("Transport protégé absent : arrêt sans repli direct")
if DV_NETWORK_MODE == "protected" and DV_PROTECTED_EGRESS_PROFILE not in {"TOR", "VPN_EXISTING"}:
    raise ImproperlyConfigured("Le profil protégé doit être TOR ou VPN_EXISTING")

DV_ANALYSIS_PROVIDER_ENDPOINT = os.environ.get("DV_ANALYSIS_PROVIDER_ENDPOINT", "").strip()
DV_ANALYSIS_PROVIDER_TOKEN = os.environ.get("DV_ANALYSIS_PROVIDER_TOKEN", "")
DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS = int(os.environ.get("DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS", "60"))
DV_ANALYSIS_PROVIDER_MAX_INPUT_BYTES = int(os.environ.get("DV_ANALYSIS_PROVIDER_MAX_INPUT_BYTES", "262144"))
DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES = int(os.environ.get("DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES", "131072"))
if not 2 <= DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS <= 300:
    raise ImproperlyConfigured("DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS hors bornes")
if not 1024 <= DV_ANALYSIS_PROVIDER_MAX_INPUT_BYTES <= 1_048_576:
    raise ImproperlyConfigured("DV_ANALYSIS_PROVIDER_MAX_INPUT_BYTES hors bornes")
if not 1024 <= DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES <= 524_288:
    raise ImproperlyConfigured("DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES hors bornes")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend" if DV_ENV in {"local", "test"} else os.environ.get(
    "DV_EMAIL_BACKEND", "django.core.mail.backends.dummy.EmailBackend"
)

# --- PRIVATE-ADMIN-1 : frontière d'entrée privée (contrat de config, sans adresses dans Git) ---
# Modes : open (local/test uniquement) | header (proxy de confiance) | closed (fail-closed).
# Jamais fonder l'autorisation sur Host ou X-Forwarded-For client.
_default_private_entry = "open" if DV_ENV in {"local", "test"} else "closed"
DV_PRIVATE_ENTRY_MODE = os.environ.get("DV_PRIVATE_ENTRY_MODE", _default_private_entry).strip().lower()
if DV_PRIVATE_ENTRY_MODE not in {"open", "header", "closed"}:
    raise ImproperlyConfigured("DV_PRIVATE_ENTRY_MODE doit valoir open, header ou closed")
if DV_PRIVATE_ENTRY_MODE == "open" and DV_ENV not in {"local", "test"}:
    raise ImproperlyConfigured("DV_PRIVATE_ENTRY_MODE=open interdit hors local/test")
_trust_private = os.environ.get("DV_TRUST_PRIVATE_ENTRY_PROXY", "0")
if _trust_private not in {"0", "1"}:
    raise ImproperlyConfigured("DV_TRUST_PRIVATE_ENTRY_PROXY doit valoir 0 ou 1")
DV_TRUST_PRIVATE_ENTRY_PROXY = _trust_private == "1"
# META Django pour l'en-tête injecté/écrasé par le proxy (ex. X-DV-Private-Entry → HTTP_X_DV_PRIVATE_ENTRY).
DV_PRIVATE_ENTRY_HEADER_META = os.environ.get(
    "DV_PRIVATE_ENTRY_HEADER_META", "HTTP_X_DV_PRIVATE_ENTRY"
).strip()
DV_PRIVATE_ENTRY_EXPECTED_VALUE = os.environ.get("DV_PRIVATE_ENTRY_EXPECTED_VALUE", "1")
if DV_ENV in {"private", "production"} and DV_PRIVATE_ENTRY_MODE == "header" and not DV_TRUST_PRIVATE_ENTRY_PROXY:
    raise ImproperlyConfigured(
        "DV_PRIVATE_ENTRY_MODE=header exige DV_TRUST_PRIVATE_ENTRY_PROXY=1 "
        "(le proxy doit écraser l'en-tête ; jamais faire confiance au client)"
    )
# Qualifié EFFECTIF seulement après contrôle NAS (proxy/ACL) — hors lot cloud.
DV_PRIVATE_ADMIN_NAS_QUALIFIED = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "atelier": {"handlers": ["console"], "level": os.environ.get("DV_LOG_LEVEL", "INFO"), "propagate": False}
    },
}
