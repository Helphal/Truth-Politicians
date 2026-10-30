"""Destinations configurables : aucune requête réseau réelle dans cette suite."""
import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from atelier.services.analysis_provider import AnalysisProviderUnavailable, _endpoint
from atelier.services.wordpress import WordPressUnavailable, _post_packet


@override_settings(DV_ENV="test", DV_NETWORK_MODE="direct")
class PublicationDestinationTests(SimpleTestCase):
    def configuration(self, *, endpoint="https://publication.example.org/events", allowed=""):
        return patch.dict(
            os.environ,
            {
                "DV_WORDPRESS_LOOPBACK_TEST": "0",
                "DV_WORDPRESS_ENDPOINT": endpoint,
                "DV_WORDPRESS_ALLOWED_HOSTS": allowed,
                "DV_WORDPRESS_USERNAME": "synthetic-user",
                "DV_WORDPRESS_APP_PASSWORD": "synthetic-not-a-secret",
            },
            clear=False,
        )

    def test_missing_allowlist_refuses_before_dns_or_transport(self):
        with self.configuration(), patch("atelier.services.wordpress.validate_source_url") as resolver:
            with self.assertRaisesMessage(WordPressUnavailable, "wordpress_endpoint_not_allowed"):
                _post_packet({"event_id": "test-only"})
        resolver.assert_not_called()

    def test_explicit_destination_uses_existing_pinned_https_transport(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"status":"synthetic"}'
        with self.configuration(allowed="publication.example.org"), patch(
            "atelier.services.wordpress.validate_source_url",
            return_value=("publication.example.org", "/events", ("192.0.2.44",)),
        ) as resolver, patch(
            "atelier.services.wordpress._PinnedHTTPSConnection", return_value=connection
        ) as transport:
            self.assertEqual(_post_packet({"event_id": "test-only"}), {"status": "synthetic"})
        resolver.assert_called_once_with(
            "https://publication.example.org/events", allowed_hosts={"publication.example.org"}
        )
        transport.assert_called_once_with("publication.example.org", "192.0.2.44", timeout=20)
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Idempotency-Key"], "test-only")
        connection.close.assert_called_once()

    def test_allowlist_never_authorizes_http_credentials_other_hosts_or_fragments(self):
        for endpoint in (
            "http://publication.example.org/events",
            "https://publication.example.org.attacker.invalid/events",
            "https://user:password@publication.example.org/events",
            "https://publication.example.org:8443/events",
            "https://publication.example.org/events#fragment",
        ):
            with self.subTest(endpoint=endpoint), self.configuration(
                endpoint=endpoint, allowed="publication.example.org"
            ), patch("atelier.services.wordpress.validate_source_url") as resolver:
                with self.assertRaisesMessage(WordPressUnavailable, "wordpress_endpoint_not_allowed"):
                    _post_packet({"event_id": "test-only"})
                resolver.assert_not_called()

    @override_settings(DV_NETWORK_MODE="disabled")
    def test_configured_destination_does_not_bypass_disabled_network(self):
        with self.configuration(allowed="publication.example.org"), patch(
            "atelier.services.wordpress.validate_source_url"
        ) as resolver:
            with self.assertRaisesMessage(WordPressUnavailable, "network_disabled"):
                _post_packet({"event_id": "test-only"})
        resolver.assert_not_called()


class AnalysisProviderEndpointTests(SimpleTestCase):
    @override_settings(DV_ANALYSIS_PROVIDER_ENDPOINT="https://analysis.example.org/custom/prepare")
    def test_uses_full_configured_service_url(self):
        self.assertEqual(_endpoint(), "https://analysis.example.org/custom/prepare")

    @override_settings(DV_ANALYSIS_PROVIDER_ENDPOINT="http://analysis.example.org/custom/prepare")
    def test_refuses_unencrypted_public_service(self):
        with self.assertRaisesMessage(AnalysisProviderUnavailable, "analysis_provider_https_required"):
            _endpoint()
