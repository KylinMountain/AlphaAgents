import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.world_news import get_world_news_fn, _parse_rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Trump announces new tariffs on China</title>
    <description>The US president announced sweeping tariffs.</description>
    <pubDate>Wed, 02 Apr 2026 12:00:00 GMT</pubDate>
    <link>https://example.com/1</link>
  </item>
  <item>
    <title>Musk acquires another company</title>
    <description>Elon Musk has completed yet another acquisition.</description>
    <pubDate>Wed, 02 Apr 2026 11:00:00 GMT</pubDate>
    <link>https://example.com/2</link>
  </item>
  <item>
    <title>Oil prices surge amid Middle East tensions</title>
    <description>Crude oil hit $90 per barrel.</description>
    <pubDate>Wed, 02 Apr 2026 10:00:00 GMT</pubDate>
    <link>https://example.com/3</link>
  </item>
</channel>
</rss>"""


def test_parse_rss():
    items = _parse_rss(SAMPLE_RSS, "Test")
    assert len(items) == 3
    assert items[0]["title"] == "Trump announces new tariffs on China"
    assert items[0]["source"] == "Test"


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml at all", "Bad")
    assert items == []


def _mock_client_session():
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_resp

    cm = MagicMock()
    cm.__enter__ = lambda s: mock_client
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_get_world_news_returns_json():
    with patch("alpha_agents.tools.world_news.client_session", return_value=_mock_client_session()):
        result = json.loads(get_world_news_fn(limit=10))
        assert result["count"] > 0
        assert result["news"][0]["title"] == "Trump announces new tariffs on China"


def test_get_world_news_keyword_filter():
    with patch("alpha_agents.tools.world_news.client_session", return_value=_mock_client_session()):
        result = json.loads(get_world_news_fn(keyword="trump"))
        assert result["count"] > 0
        assert all("trump" in n["title"].lower() for n in result["news"])


def test_get_world_news_respects_limit():
    with patch("alpha_agents.tools.world_news.client_session", return_value=_mock_client_session()):
        result = json.loads(get_world_news_fn(limit=1))
        assert result["count"] == 1
