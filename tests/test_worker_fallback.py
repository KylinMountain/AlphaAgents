"""CF Worker health probe and direct fallback.

Deployed on a host that could not reach workers.dev, every overseas
source burned three retries against an unreachable proxy and the ingest
task timed out — while those same sites answered directly from the same
container. The proxy dodges anti-scraping, so losing it costs quality;
losing every overseas source costs everything.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alpha_agents import http_client


@pytest.fixture(autouse=True)
def _reset_probe():
    """The probe caches per process; tests must not inherit each other's."""
    http_client._WORKER_HEALTHY = None
    yield
    http_client._WORKER_HEALTHY = None


class TestWorkerReachable:
    def test_reachable_worker_is_used(self):
        with patch.object(http_client, "_CF_WORKER_URL", "https://w.example"), \
             patch.object(http_client, "httpx") as mock_httpx:
            mock_httpx.Client.return_value.__enter__.return_value.get.return_value = MagicMock()
            assert http_client._worker_reachable() is True

    def test_unreachable_worker_reports_false(self):
        with patch.object(http_client, "_CF_WORKER_URL", "https://w.example"), \
             patch.object(http_client, "httpx") as mock_httpx:
            mock_httpx.Client.side_effect = httpx.ConnectError("unreachable")
            assert http_client._worker_reachable() is False

    def test_no_worker_configured_is_false(self):
        with patch.object(http_client, "_CF_WORKER_URL", ""):
            assert http_client._worker_reachable() is False

    def test_probe_runs_once_and_caches(self):
        """A probe per request would add a round trip to every fetch."""
        with patch.object(http_client, "_CF_WORKER_URL", "https://w.example"), \
             patch.object(http_client, "httpx") as mock_httpx:
            mock_httpx.Client.return_value.__enter__.return_value.get.return_value = MagicMock()
            for _ in range(5):
                http_client._worker_reachable()
            assert mock_httpx.Client.call_count == 1

    def test_negative_result_is_cached_too(self):
        with patch.object(http_client, "_CF_WORKER_URL", "https://w.example"), \
             patch.object(http_client, "httpx") as mock_httpx:
            mock_httpx.Client.side_effect = httpx.ConnectError("nope")
            for _ in range(4):
                assert http_client._worker_reachable() is False
            assert mock_httpx.Client.call_count == 1


class TestRouting:
    """Which path a URL takes, given worker health and domain."""

    def _route(self, url, worker_url, healthy):
        """Return True when fetch would use the worker."""
        domain = http_client._extract_domain(url)
        with patch.object(http_client, "_CF_WORKER_URL", worker_url), \
             patch.object(http_client, "_worker_reachable", return_value=healthy):
            return (bool(http_client._CF_WORKER_URL)
                    and not http_client._is_domestic(domain)
                    and http_client._worker_reachable())

    def test_overseas_uses_worker_when_healthy(self):
        assert self._route("https://www.federalreserve.gov/x",
                           "https://w.example", True) is True

    def test_overseas_falls_back_to_direct_when_worker_is_down(self):
        assert self._route("https://www.federalreserve.gov/x",
                           "https://w.example", False) is False

    def test_domestic_never_uses_the_worker(self):
        for url in ("https://www.jin10.com/x", "https://zhibo.sina.com.cn/x",
                    "https://www.pbc.gov.cn/x"):
            assert self._route(url, "https://w.example", True) is False

    def test_no_worker_configured_means_direct(self):
        assert self._route("https://www.federalreserve.gov/x", "", True) is False
