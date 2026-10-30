from datetime import UTC, datetime
from unittest.mock import patch

from django.test import override_settings

from atelier.services.network_policy import NetworkBlocked, require_collection_transport, require_direct_transport
from atelier.services.protected_transport import ProtectedTransportError, _endpoint
from atelier.management.commands.run_private_scheduler import next_delay_seconds

from .support import AtelierTestCase


class Suite003TransportTests(AtelierTestCase):
    def test_private_scheduler_computes_next_daily_slot_without_overlap_loop(self):
        self.assertEqual(next_delay_seconds(datetime(2026, 9, 14, 1, 0, tzinfo=UTC)), 17 * 60)

    @override_settings(
        DV_NETWORK_MODE="protected",
        DV_PROTECTED_EGRESS_CONFIRMED=True,
        DV_PROTECTED_TRANSPORT_ENDPOINT="",
        DV_PROTECTED_EGRESS_PROFILE="TOR",
    )
    def test_protected_mode_without_endpoint_fails_closed(self):
        with self.assertRaisesMessage(NetworkBlocked, "protected_transport_endpoint_missing_no_direct_fallback"):
            require_collection_transport()
        with patch("socket.getaddrinfo") as dns:
            with self.assertRaises(NetworkBlocked):
                require_direct_transport()
            dns.assert_not_called()

    @override_settings(
        DV_NETWORK_MODE="protected",
        DV_PROTECTED_EGRESS_CONFIRMED=True,
        DV_PROTECTED_TRANSPORT_ENDPOINT="https://worker.example.invalid:8443",
        DV_PROTECTED_EGRESS_PROFILE="DIRECT_DISCLOSED",
    )
    def test_direct_disclosed_profile_is_not_accepted_as_protected(self):
        with self.assertRaisesMessage(NetworkBlocked, "protected_transport_profile_not_protected"):
            require_collection_transport()

    @override_settings(DV_PROTECTED_TRANSPORT_ENDPOINT="http://worker.example.invalid:8443")
    def test_protected_endpoint_requires_verified_https(self):
        with self.assertRaisesMessage(ProtectedTransportError, "protected_transport_endpoint_invalid"):
            _endpoint()
