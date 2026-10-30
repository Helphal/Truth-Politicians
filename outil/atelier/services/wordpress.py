"""Client WordPress candidat. Aucun endpoint n'est inventé ou activé par défaut."""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from pathlib import Path
from atelier.models import OutboxEvent, EditorialDossier

from .canonical import canonical_bytes, sha256_digest
from .collector import _PinnedHTTPSConnection, validate_source_url


class WordPressUnavailable(RuntimeError):
    pass


def _extract_transport_receipt(parsed: dict) -> dict:
    """Le site renvoie {receipt, duplicate} ; send_packet attend le reçu seul."""
    from .receipts import receipt_verification_view

    if not isinstance(parsed, dict):
        raise WordPressUnavailable("wordpress_response_invalid")
    inner = parsed.get("receipt")
    if not isinstance(inner, dict):
        return receipt_verification_view(parsed)
    receipt = dict(inner)
    if parsed.get("duplicate") is True:
        receipt["_transport_duplicate"] = True
    return receipt


def _post_packet(packet: dict, *, timeout: float = 20) -> dict:
    from .wordpress_loopback import loopback_transport_enabled, post_packet_loopback

    if loopback_transport_enabled():
        return _extract_transport_receipt(post_packet_loopback(packet, timeout=timeout))
    endpoint = os.environ.get("DV_WORDPRESS_ENDPOINT", "")
    username = os.environ.get("DV_WORDPRESS_USERNAME", "")
    password = os.environ.get("DV_WORDPRESS_APP_PASSWORD", "")
    if not endpoint or not username or not password:
        raise WordPressUnavailable("wordpress_configuration_missing")
    # La configuration manquante reste diagnostiquée, mais aucune résolution ne précède cette autorité.
    from .network_policy import require_direct_transport, NetworkBlocked
    try:
        require_direct_transport()
    except NetworkBlocked as exc:
        raise WordPressUnavailable(str(exc)) from exc
    # Chaque installation autorise explicitement ses propres destinataires.
    # Une URL configurée seule ne suffit jamais à ouvrir une sortie réseau.
    allowed_hosts = {
        host.strip().lower()
        for host in os.environ.get("DV_WORDPRESS_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise WordPressUnavailable("wordpress_endpoint_not_allowed")
    except (ValueError, UnicodeError) as exc:
        raise WordPressUnavailable("wordpress_endpoint_not_allowed") from exc
    # Réutilise la résolution/pinning sans mutation de la configuration globale.
    host, path, addresses = validate_source_url(
        endpoint, allowed_hosts=allowed_hosts
    )
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    body = canonical_bytes(packet)
    event_id = str(packet.get("event_id") or "")
    if not event_id:
        raise WordPressUnavailable("wordpress_packet_missing_event_id")
    last_error = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(host, address, timeout=timeout)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Basic {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "TruthPoliticians-private-exporter/0.1",
                    "Idempotency-Key": event_id,
                },
            )
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise WordPressUnavailable("wordpress_response_too_large")
            if response.status not in {200, 201, 202}:
                raise WordPressUnavailable(f"wordpress_http_{response.status}")
            parsed_body = json.loads(raw)
            if not isinstance(parsed_body, dict):
                raise WordPressUnavailable("wordpress_response_invalid")
            return _extract_transport_receipt(parsed_body)
        except Exception as exc:
            last_error = exc
        finally:
            connection.close()
    raise WordPressUnavailable("wordpress_connection_failed") from last_error


def send_packet(packet: dict, *, timeout: float = 20) -> dict:
    # Conserver le diagnostic historique sans jamais résoudre le domaine en cas d'arrêt.
    if not all(os.environ.get(name) for name in ("DV_WORDPRESS_ENDPOINT", "DV_WORDPRESS_USERNAME", "DV_WORDPRESS_APP_PASSWORD")):
        raise WordPressUnavailable("wordpress_configuration_missing")
    from .network_policy import require_direct_transport, NetworkBlocked
    try:
        require_direct_transport()
    except NetworkBlocked as exc:
        raise WordPressUnavailable(str(exc)) from exc
    if not settings.DV_PUBLICATION_SEND_AUTHORIZED or not settings.DV_RECEIVER_STATE_RECONCILED:
        raise WordPressUnavailable("sending_not_authorized_or_receiver_not_reconciled")
    db = Path(str(settings.DATABASES["default"]["NAME"]))
    if any((path / ".dv-restored-unreconciled").exists() for path in (settings.RUNTIME_DIR, db.parent, db.parent.parent)):
        raise WordPressUnavailable("restored_database_requires_receiver_reconciliation")
    expected_environment = "test" if settings.DV_ENV in {"local", "test"} else settings.DV_ENV
    if not isinstance(packet, dict) or packet.get("environment") != expected_environment:
        raise WordPressUnavailable("wrong_sending_environment")
    from .publication import packet_for_event, invalidate_event
    from .receipts import verify_receipt
    failure, receipt = None, None
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().filter(event_id=packet.get("event_id")).first()
        if event is None:
            raise WordPressUnavailable("event_not_in_current_outbox")
        EditorialDossier.objects.select_for_update().get(pk=event.dossier_id)
        if event.attempts >= 3 or event.state not in {OutboxEvent.State.PENDING, OutboxEvent.State.SENDING}:
            raise WordPressUnavailable("event_not_sendable")
        event.attempts += 1
        event.state = OutboxEvent.State.SENDING
        event.save(update_fields=["attempts", "state", "updated_at"])
        # Le verrou d'écriture SQLite est pris avant de relire revue, sources et instantané.
        try:
            current = packet_for_event(event)
            if sha256_digest(current) != sha256_digest(packet):
                raise ValidationError("exported_packet_not_current")
            receipt = _post_packet(packet, timeout=timeout)
            if not verify_receipt(receipt, packet):
                raise WordPressUnavailable("receipt_not_bound_to_event")
        except ValidationError as exc:
            failure = str(exc.messages[0])
            invalidate_event(event, failure)
        except WordPressUnavailable as exc:
            failure = str(exc)
            event.state = OutboxEvent.State.PENDING if event.attempts < 3 else OutboxEvent.State.FAILED
            event.last_error_code = failure[:100]
            event.save(update_fields=["state", "last_error_code", "updated_at"])
        else:
            if event.operation == OutboxEvent.Operation.SUSPEND:
                from .reconciliation import acknowledge_withdrawal
                acknowledge_withdrawal(event, packet, receipt)
            event.state = OutboxEvent.State.DELIVERED
            event.delivered_at = timezone.now()
            event.save(update_fields=["state", "delivered_at", "updated_at"])
    if failure:
        raise WordPressUnavailable(failure)
    return receipt
