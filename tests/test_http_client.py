from unittest.mock import patch, MagicMock

from alpha_agents.http_client import (
    random_ua,
    get_headers,
    fetch,
    client_session,
    _USER_AGENTS,
)


def test_random_ua_returns_string():
    ua = random_ua()
    assert isinstance(ua, str)
    assert ua in _USER_AGENTS


def test_get_headers_has_user_agent():
    headers = get_headers()
    assert "User-Agent" in headers
    assert headers["User-Agent"] in _USER_AGENTS
    assert "Accept" in headers


def test_get_headers_merges_extra():
    headers = get_headers({"X-Custom": "test"})
    assert headers["X-Custom"] == "test"
    assert "User-Agent" in headers


def test_fetch_makes_request():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.request.return_value = mock_resp

    with patch("alpha_agents.http_client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: mock_client
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with patch("alpha_agents.http_client._throttle.wait"):
            resp = fetch("https://example.com", throttle=True)
            assert resp == mock_resp


def test_fetch_retries_on_server_error():
    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.request.side_effect = [mock_resp_500, mock_resp_200]

    with patch("alpha_agents.http_client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: mock_client
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with patch("alpha_agents.http_client._throttle.wait"), \
             patch("alpha_agents.http_client.time.sleep"):
            resp = fetch("https://example.com", max_retries=1)
            assert resp == mock_resp_200


def test_client_session_returns_client():
    with patch("alpha_agents.http_client.httpx.Client") as MockClient:
        mock_client = MagicMock()
        MockClient.return_value.__enter__ = lambda s: mock_client
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with client_session() as client:
            assert client == mock_client
