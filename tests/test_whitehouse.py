import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.whitehouse import get_whitehouse_fn, _parse_rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>The White House</title>
  <item>
    <title>Executive Order on Protecting American Industry</title>
    <description>The President signed an executive order to safeguard domestic manufacturing.</description>
    <pubDate>Thu, 02 Apr 2026 14:00:00 GMT</pubDate>
    <link>https://www.whitehouse.gov/briefing-room/executive-order-1</link>
  </item>
  <item>
    <title>Statement on Federal Reserve Interest Rate Decision</title>
    <description>The White House issued a statement regarding the latest Fed decision.</description>
    <pubDate>Thu, 02 Apr 2026 12:00:00 GMT</pubDate>
    <link>https://www.whitehouse.gov/briefing-room/statement-fed-rate</link>
  </item>
  <item>
    <title>Remarks by the President on Infrastructure Investment</title>
    <description>The President delivered remarks on a new infrastructure spending plan.</description>
    <pubDate>Wed, 01 Apr 2026 10:00:00 GMT</pubDate>
    <link>https://www.whitehouse.gov/briefing-room/remarks-infrastructure</link>
  </item>
</channel>
</rss>"""


def test_parse_rss():
    items = _parse_rss(SAMPLE_RSS)
    assert len(items) == 3
    assert items[0]["title"] == "Executive Order on Protecting American Industry"
    assert items[0]["source"] == "White House"
    assert items[0]["link"] == "https://www.whitehouse.gov/briefing-room/executive-order-1"


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml at all")
    assert items == []


def test_parse_rss_empty_feed():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>"""
    items = _parse_rss(xml)
    assert items == []


def test_get_whitehouse_returns_json():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.whitehouse.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_whitehouse_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "Executive Order on Protecting American Industry"
        assert result["news"][0]["source"] == "White House"


def test_get_whitehouse_keyword_filter():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.whitehouse.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_whitehouse_fn(keyword="infrastructure"))
        assert result["count"] == 1
        assert "infrastructure" in result["news"][0]["title"].lower()


def test_get_whitehouse_respects_limit():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.whitehouse.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_whitehouse_fn(limit=1))
        assert result["count"] == 1


def test_get_whitehouse_handles_fetch_error():
    with patch("alpha_agents.tools.whitehouse.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.side_effect = httpx.ConnectError("Connection refused")

        result = json.loads(get_whitehouse_fn())
        assert result["count"] == 0
        assert result["news"] == []


import httpx
