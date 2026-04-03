import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.sec import get_sec_news_fn, _parse_rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>SEC Press Releases</title>
  <item>
    <title>SEC Charges Company with Fraud</title>
    <description>The SEC today charged a company with securities fraud related to misleading investors.</description>
    <pubDate>Thu, 02 Apr 2026 16:00:00 GMT</pubDate>
    <link>https://www.sec.gov/news/press-release/2026-01</link>
  </item>
  <item>
    <title>SEC Proposes New Rules for Market Transparency</title>
    <description>The Commission proposed amendments to enhance transparency in equity markets.</description>
    <pubDate>Thu, 02 Apr 2026 14:00:00 GMT</pubDate>
    <link>https://www.sec.gov/news/press-release/2026-02</link>
  </item>
  <item>
    <title>SEC Settles Insider Trading Case</title>
    <description>The SEC announced a settlement with individuals involved in insider trading of tech stocks.</description>
    <pubDate>Wed, 01 Apr 2026 10:00:00 GMT</pubDate>
    <link>https://www.sec.gov/news/press-release/2026-03</link>
  </item>
</channel>
</rss>"""


def test_parse_rss():
    items = _parse_rss(SAMPLE_RSS)
    assert len(items) == 3
    assert items[0]["title"] == "SEC Charges Company with Fraud"
    assert items[0]["source"] == "SEC"
    assert items[0]["link"] == "https://www.sec.gov/news/press-release/2026-01"


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml at all")
    assert items == []


def test_parse_rss_empty_feed():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>"""
    items = _parse_rss(xml)
    assert items == []


def _mock_fetch(text):
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def test_get_sec_news_returns_json():
    with patch("alpha_agents.tools.sec.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_sec_news_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "SEC Charges Company with Fraud"
        assert result["news"][0]["source"] == "SEC"


def test_get_sec_news_keyword_filter():
    with patch("alpha_agents.tools.sec.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_sec_news_fn(keyword="insider trading"))
        assert result["count"] == 1
        assert "insider trading" in result["news"][0]["title"].lower()


def test_get_sec_news_respects_limit():
    with patch("alpha_agents.tools.sec.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_sec_news_fn(limit=1))
        assert result["count"] == 1


def test_get_sec_news_handles_fetch_error():
    with patch("alpha_agents.tools.sec.fetch", side_effect=Exception("Connection refused")):
        result = json.loads(get_sec_news_fn())
        assert result["count"] == 0
        assert result["news"] == []
