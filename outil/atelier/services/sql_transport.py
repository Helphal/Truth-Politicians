"""Explicit SQL transport: verified TLS, or an explicit loopback laboratory."""
from pathlib import Path
import ipaddress


class SQLTransportError(ValueError):
    pass


def policy(config):
    host = config.get('HOST')
    if not isinstance(host, str) or not host or any(c in host for c in '/\\@ \t\r\n'):
        raise SQLTransportError('sql_explicit_host_required')
    transport = config.get('SQL_TRANSPORT')
    if not isinstance(transport, dict):
        raise SQLTransportError('sql_transport_policy_required')
    mode = transport.get('mode')
    if mode == 'plaintext_loopback':
        if set(transport) != {'mode'}:
            raise SQLTransportError('sql_transport_fields_invalid')
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.lower() == 'localhost'
        if not local:
            raise SQLTransportError('sql_plaintext_remote_refused')
        return {'mode': mode}
    if mode != 'tls_verify_identity' or set(transport) != {'mode', 'ca'}:
        raise SQLTransportError('sql_verified_tls_policy_required')
    if host.lower() == 'localhost':
        # Connector/C treats this special alias as its loopback peer on Windows.
        # Require the explicit identity covered by the received certificate.
        raise SQLTransportError('sql_tls_localhost_alias_refused_use_exact_identity')
    ca = Path(transport['ca']) if isinstance(transport['ca'], str) else None
    if ca is None or not ca.is_absolute() or not ca.is_file():
        raise SQLTransportError('sql_explicit_ca_file_required')
    return {'mode': mode, 'ca': str(ca.resolve())}


def mysql_options(config):
    selected = policy(config)
    if selected['mode'] == 'plaintext_loopback':
        return {'ssl_mode': 'DISABLED', 'local_infile': False}
    return {'ssl_mode': 'VERIFY_IDENTITY', 'ssl': {'ca': selected['ca']}, 'local_infile': False}


def cli_options(config):
    selected = policy(config)
    if selected['mode'] == 'plaintext_loopback':
        return ['--skip-ssl']
    return ['--ssl', '--ssl-ca=' + selected['ca'], '--ssl-verify-server-cert',
            '--tls-version=TLSv1.2,TLSv1.3']


def verify_session(raw_connection, config):
    selected = policy(config)
    with raw_connection.cursor() as cursor:
        cursor.execute("SHOW SESSION STATUS WHERE Variable_name IN ('Ssl_version','Ssl_cipher')")
        status = dict(cursor.fetchall())
    version, cipher = status.get('Ssl_version', ''), status.get('Ssl_cipher', '')
    if selected['mode'] == 'tls_verify_identity' and (version not in {'TLSv1.2', 'TLSv1.3'} or not cipher):
        raw_connection.close()
        raise SQLTransportError('sql_verified_tls_session_required')
    if selected['mode'] == 'plaintext_loopback' and version:
        raw_connection.close()
        raise SQLTransportError('sql_transport_session_mismatch')
    return {'policy': selected['mode'], 'tls_version': version, 'cipher': cipher}


def django_connection_guard(sender, connection, **kwargs):
    if connection.vendor == 'mysql':
        verify_session(connection.connection, connection.settings_dict)


def install_django_guard():
    from django.db.backends.signals import connection_created
    connection_created.connect(django_connection_guard,
                               dispatch_uid=__name__ + '.verified_sql_transport', weak=False)
