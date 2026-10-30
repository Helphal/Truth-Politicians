"""Explicit SYNTH MariaDB qualification only; no source settings or service."""
import os
from django.core.exceptions import ImproperlyConfigured
from dv_project.settings import *  # noqa: F401,F403
from atelier.services.sql_transport import install_django_guard, mysql_options

if os.environ.get('DV_SQL_QUAL_SCOPE') != 'SYNTH':
    raise ImproperlyConfigured('SYNTH MariaDB qualification scope required')
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.mysql', 'NAME': os.environ['DV_SQL_QUAL_NAME'],
    'USER': os.environ['DV_SQL_QUAL_USER'], 'PASSWORD': os.environ['DV_SQL_QUAL_PASSWORD'],
    'HOST': os.environ['DV_SQL_QUAL_HOST'], 'PORT': int(os.environ['DV_SQL_QUAL_PORT']),
    'OPTIONS': {'charset': 'utf8mb4', 'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"},
    'SQL_TRANSPORT': {'mode': 'tls_verify_identity', 'ca': os.environ['DV_SQL_QUAL_TLS_CA']},
    'TEST': {'NAME': os.environ.get('DV_SQL_QUAL_TEST_NAME', 'dv_synth_tests'), 'CHARSET': 'utf8mb4'},
}}
DATABASES['default']['OPTIONS'].update(mysql_options(DATABASES['default']))
install_django_guard()
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
