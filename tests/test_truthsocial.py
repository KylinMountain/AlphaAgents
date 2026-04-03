import json
from unittest.mock import patch, MagicMock, call

from alpha_agents.tools.truthsocial import (
    get_social_media_fn,
    _parse_rss,
    _fetch_feed,
)

SAMPLE_TRUMP_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Donald Trump - Truth Social</title>
  <item>
    <title>We are WINNING like never before!</title>
    <description>America is back and stronger than ever. Trade deals coming soon!</description>
    <pubDate>Thu, 02 Apr 2026 18:00:00 GMT</pubDate>
    <link>https://truthsocial.com/@realDonaldTrump/posts/123</link>
  </item>
  <item>
    <title>Big announcement on tariffs coming tomorrow</title>
    <description>Stay tuned for major trade policy news that will change everything.</description>
    <pubDate>Thu, 02 Apr 2026 12:00:00 GMT</pubDate>
    <link>https://truthsocial.com/@realDonaldTrump/posts/122</link>
  </item>
</channel>
</rss>"""

SAMPLE_MUSK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Elon Musk - X</title>
  <item>
    <title>Starship orbital flight test successful</title>
    <description>Full stack Starship completed its orbital test flight. Next stop: Mars!</description>
    <pubDate>Thu, 02 Apr 2026 20:00:00 GMT</pubDate>
    <link>https://x.com/elonmusk/status/999</link>
  </item>
  <item>
    <title>Tesla Q1 deliveries exceed expectations</title>
    <description>Record quarter for Tesla with strong demand across all models.</description>
    <pubDate>Thu, 02 Apr 2026 15:00:00 GMT</pubDate>
    <link>https://x.com/elonmusk/status/998</link>
  </item>
</channel>
</rss>"""


def test_parse_rss_trump():
    items = _parse_rss(SAMPLE_TRUMP_RSS, "Truth Social", "Donald Trump")
    assert len(items) == 2
    assert items[0]["source"] == "Truth Social"
    assert items[0]["author"] == "Donald Trump"
    assert items[0]["title"] == "We are WINNING like never before!"


def test_parse_rss_musk():
    items = _parse_rss(SAMPLE_MUSK_RSS, "X/Twitter", "Elon Musk")
    assert len(items) == 2
    assert items[0]["source"] == "X/Twitter"
    assert items[0]["author"] == "Elon Musk"


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml", "X/Twitter", "Elon Musk")
    assert items == []


def test_parse_rss_empty_feed():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>"""
    items = _parse_rss(xml, "Truth Social", "Donald Trump")
    assert items == []


def _mock_fetch(text):
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def test_fetch_feed_primary_success():
    with patch("alpha_agents.tools.truthsocial.fetch", return_value=_mock_fetch(SAMPLE_TRUMP_RSS)):
        items = _fetch_feed(
            "https://primary.example.com",
            "https://fallback.example.com",
            "Truth Social",
            "Donald Trump",
        )
        assert len(items) == 2
        assert items[0]["author"] == "Donald Trump"


def test_fetch_feed_falls_back():
    """When primary fails, fallback is tried."""
    def side_effect(url):
        if "primary" in url:
            raise Exception("Primary down")
        return _mock_fetch(SAMPLE_TRUMP_RSS)

    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=side_effect):
        items = _fetch_feed(
            "https://primary.example.com",
            "https://fallback.example.com",
            "Truth Social",
            "Donald Trump",
        )
        assert len(items) == 2


def test_fetch_feed_both_fail():
    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=Exception("All down")):
        items = _fetch_feed(
            "https://primary.example.com",
            "https://fallback.example.com",
            "Truth Social",
            "Donald Trump",
        )
        assert items == []


def test_get_social_media_returns_json():
    def side_effect(url):
        if "truthsocial" in url:
            return _mock_fetch(SAMPLE_TRUMP_RSS)
        if "twitter" in url:
            return _mock_fetch(SAMPLE_MUSK_RSS)
        raise Exception("Unknown URL")

    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(limit=20))
        assert result["count"] == 4
        sources = {p["source"] for p in result["news"]}
        assert "Truth Social" in sources
        assert "X/Twitter" in sources


def test_get_social_media_keyword_filter():
    def side_effect(url):
        if "truthsocial" in url:
            return _mock_fetch(SAMPLE_TRUMP_RSS)
        if "twitter" in url:
            return _mock_fetch(SAMPLE_MUSK_RSS)
        raise Exception("Unknown URL")

    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(keyword="tariffs"))
        assert result["count"] == 1
        assert "tariffs" in result["news"][0]["title"].lower()


def test_get_social_media_respects_limit():
    def side_effect(url):
        if "truthsocial" in url:
            return _mock_fetch(SAMPLE_TRUMP_RSS)
        if "twitter" in url:
            return _mock_fetch(SAMPLE_MUSK_RSS)
        raise Exception("Unknown URL")

    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(limit=2))
        assert result["count"] == 2


def test_get_social_media_handles_all_errors():
    with patch("alpha_agents.tools.truthsocial.fetch", side_effect=Exception("Network error")):
        result = json.loads(get_social_media_fn())
        assert result["count"] == 0
        assert result["news"] == []
