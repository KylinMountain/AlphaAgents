"""Tests for tech news RSS source."""

import json
from unittest.mock import patch, MagicMock

from alpha_agents.sources.tech_news import (
    get_tech_news_fn,
    _parse_rss,
    _strip_cdata,
    TECH_FEEDS,
)

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>TechCrunch</title>
    <item>
      <title>NVIDIA unveils new H300 chip at GTC 2026</title>
      <description>&lt;p&gt;NVIDIA announced its next-gen H300 GPU...&lt;/p&gt;</description>
      <pubDate>Fri, 03 Apr 2026 23:25:01 +0000</pubDate>
      <link>https://techcrunch.com/2026/04/03/nvidia-h300/</link>
      <category>AI</category>
      <category>Semiconductors</category>
    </item>
    <item>
      <title>Tesla shows off Optimus Gen 3 robot</title>
      <description>Tesla demonstrated its latest humanoid robot...</description>
      <pubDate>Fri, 03 Apr 2026 20:00:00 +0000</pubDate>
      <link>https://techcrunch.com/2026/04/03/tesla-optimus/</link>
      <category>Robotics</category>
    </item>
    <item>
      <title></title>
      <description>Empty title item should be skipped</description>
    </item>
  </channel>
</rss>"""


def test_strip_cdata():
    assert _strip_cdata("<![CDATA[hello]]>") == "hello"
    assert _strip_cdata("plain") == "plain"
    assert _strip_cdata("") == ""


def test_parse_rss_basic():
    items = _parse_rss(SAMPLE_RSS, "TechCrunch")
    assert len(items) == 2  # empty title skipped
    assert items[0]["title"] == "NVIDIA unveils new H300 chip at GTC 2026"
    assert items[0]["source"] == "TechCrunch"
    assert items[0]["time"] == "Fri, 03 Apr 2026 23:25:01 +0000"
    assert items[0]["link"] == "https://techcrunch.com/2026/04/03/nvidia-h300/"
    assert "AI" in items[0]["tags"]
    assert "Semiconductors" in items[0]["tags"]


def test_parse_rss_strips_html():
    items = _parse_rss(SAMPLE_RSS, "Test")
    # <p> tags should be stripped
    assert "<p>" not in items[0]["summary"]
    assert "NVIDIA announced" in items[0]["summary"]


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml at all", "Test")
    assert items == []


def test_parse_rss_empty():
    items = _parse_rss(
        '<?xml version="1.0"?><rss><channel></channel></rss>',
        "Test",
    )
    assert items == []


def test_get_tech_news_fn_success():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS

    with patch("alpha_agents.sources.tech_news.fetch", return_value=mock_resp):
        result = json.loads(get_tech_news_fn(limit=50))

    # 2 items per feed × 3 feeds = 6
    assert result["count"] == 6
    assert len(result["news"]) == 6


def test_get_tech_news_fn_limit():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS

    with patch("alpha_agents.sources.tech_news.fetch", return_value=mock_resp):
        result = json.loads(get_tech_news_fn(limit=3))

    assert result["count"] == 3


def test_get_tech_news_fn_keyword_filter():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS

    with patch("alpha_agents.sources.tech_news.fetch", return_value=mock_resp):
        result = json.loads(get_tech_news_fn(keyword="nvidia"))

    assert result["count"] >= 1
    assert all("nvidia" in n["title"].lower() for n in result["news"])


def test_get_tech_news_fn_fetch_failure():
    """All feeds fail — should return empty result."""
    with patch("alpha_agents.sources.tech_news.fetch", side_effect=Exception("timeout")):
        result = json.loads(get_tech_news_fn())

    assert result["count"] == 0
    assert result["news"] == []


def test_get_tech_news_fn_partial_failure():
    """Some feeds fail, some succeed."""
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS

    call_count = 0

    def side_effect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_resp
        raise Exception("timeout")

    with patch("alpha_agents.sources.tech_news.fetch", side_effect=side_effect):
        result = json.loads(get_tech_news_fn())

    assert result["count"] == 2  # only first feed succeeded


def test_feeds_list():
    assert len(TECH_FEEDS) >= 3
    for source_id, name, url in TECH_FEEDS:
        assert source_id
        assert name
        assert url.startswith("https://")
