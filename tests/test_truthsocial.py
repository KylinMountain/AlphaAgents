import json
from unittest.mock import patch, MagicMock

from alpha_agents.sources.truthsocial import (
    get_social_media_fn,
    _parse_google_rss,
    _parse_rss,
    _fetch_feed,
)

SAMPLE_GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Trump Truth Social - Google News</title>
  <item>
    <title>Trump posts tariff threat on Truth Social</title>
    <description>President Trump posted on Truth Social that new tariffs are coming.</description>
    <pubDate>Fri, 03 Apr 2026 10:00:00 GMT</pubDate>
    <link>https://example.com/article1</link>
    <source url="https://cnbc.com">CNBC</source>
  </item>
  <item>
    <title>Musk tweets about Tesla production numbers</title>
    <description>Elon Musk shared record Q1 deliveries on X.</description>
    <pubDate>Fri, 03 Apr 2026 09:00:00 GMT</pubDate>
    <link>https://example.com/article2</link>
    <source url="https://bbc.com">BBC</source>
  </item>
</channel>
</rss>"""

SAMPLE_POLITICAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>The Hill</title>
  <item>
    <title>Senate passes new trade bill</title>
    <description>The Senate voted to approve new trade legislation.</description>
    <pubDate>Fri, 03 Apr 2026 08:00:00 GMT</pubDate>
    <link>https://thehill.com/article1</link>
  </item>
  <item>
    <title>Trump signs executive order on tariffs</title>
    <description>New executive order imposes 25% tariffs on imports.</description>
    <pubDate>Thu, 02 Apr 2026 20:00:00 GMT</pubDate>
    <link>https://thehill.com/article2</link>
  </item>
</channel>
</rss>"""


def test_parse_google_rss():
    items = _parse_google_rss(SAMPLE_GOOGLE_RSS, "Trump Social Media")
    assert len(items) == 2
    assert items[0]["title"] == "Trump posts tariff threat on Truth Social"
    # Google News source tag is used
    assert items[0]["source"] == "CNBC"
    assert items[0]["author"] == "Trump Social Media"


def test_parse_google_rss_invalid_xml():
    items = _parse_google_rss("not xml", "Trump Social Media")
    assert items == []


def test_parse_rss():
    items = _parse_rss(SAMPLE_POLITICAL_RSS, "The Hill")
    assert len(items) == 2
    assert items[0]["source"] == "The Hill"
    assert "trade" in items[0]["title"].lower()


def test_parse_rss_invalid():
    items = _parse_rss("garbage", "Test")
    assert items == []


def _mock_fetch(text):
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def test_fetch_feed_google():
    with patch("alpha_agents.sources.truthsocial.fetch", return_value=_mock_fetch(SAMPLE_GOOGLE_RSS)):
        items = _fetch_feed("https://example.com", "Trump", is_google=True)
        assert len(items) == 2
        assert items[0]["source"] == "CNBC"


def test_fetch_feed_political():
    with patch("alpha_agents.sources.truthsocial.fetch", return_value=_mock_fetch(SAMPLE_POLITICAL_RSS)):
        items = _fetch_feed("https://example.com", "The Hill")
        assert len(items) == 2


def test_fetch_feed_error():
    with patch("alpha_agents.sources.truthsocial.fetch", side_effect=Exception("Network error")):
        items = _fetch_feed("https://example.com", "Test")
        assert items == []


def test_get_social_media_returns_json():
    def side_effect(url, **kwargs):
        if "google.com" in url:
            return _mock_fetch(SAMPLE_GOOGLE_RSS)
        return _mock_fetch(SAMPLE_POLITICAL_RSS)

    with patch("alpha_agents.sources.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(limit=20))
        assert result["count"] > 0
        # Should have both Google News and political feed items
        assert any("tariff" in n["title"].lower() for n in result["news"])


def test_get_social_media_keyword_filter():
    def side_effect(url, **kwargs):
        if "google.com" in url:
            return _mock_fetch(SAMPLE_GOOGLE_RSS)
        return _mock_fetch(SAMPLE_POLITICAL_RSS)

    with patch("alpha_agents.sources.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(keyword="tariff"))
        assert result["count"] >= 1
        assert all("tariff" in n["title"].lower() or "tariff" in n["summary"].lower()
                    for n in result["news"])


def test_get_social_media_respects_limit():
    def side_effect(url, **kwargs):
        if "google.com" in url:
            return _mock_fetch(SAMPLE_GOOGLE_RSS)
        return _mock_fetch(SAMPLE_POLITICAL_RSS)

    with patch("alpha_agents.sources.truthsocial.fetch", side_effect=side_effect):
        result = json.loads(get_social_media_fn(limit=2))
        assert result["count"] == 2


def test_get_social_media_handles_all_errors():
    with patch("alpha_agents.sources.truthsocial.fetch", side_effect=Exception("Network error")):
        result = json.loads(get_social_media_fn())
        assert result["count"] == 0
        assert result["news"] == []
