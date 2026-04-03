import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.fed import get_fed_news_fn, _parse_rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Federal Reserve Press Releases</title>
  <item>
    <title>Federal Reserve issues FOMC statement</title>
    <description>The Federal Open Market Committee decided to maintain the target range for the federal funds rate at 4-1/2 to 4-3/4 percent.</description>
    <pubDate>Thu, 02 Apr 2026 18:00:00 GMT</pubDate>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260402a.htm</link>
  </item>
  <item>
    <title>Federal Reserve Board announces approval of final rule on capital requirements</title>
    <description>The Federal Reserve Board announced its approval of a final rule to modify the capital requirements for large banks.</description>
    <pubDate>Wed, 01 Apr 2026 15:00:00 GMT</pubDate>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260401a.htm</link>
  </item>
  <item>
    <title>Quarterly report on Federal Reserve balance sheet developments</title>
    <description>The report provides updates on the Federal Reserve balance sheet and related activities.</description>
    <pubDate>Tue, 31 Mar 2026 12:00:00 GMT</pubDate>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/other20260331a.htm</link>
  </item>
</channel>
</rss>"""


def test_parse_rss():
    items = _parse_rss(SAMPLE_RSS)
    assert len(items) == 3
    assert items[0]["title"] == "Federal Reserve issues FOMC statement"
    assert items[0]["source"] == "Federal Reserve"
    assert "federalreserve.gov" in items[0]["link"]


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


def test_get_fed_news_returns_json():
    with patch("alpha_agents.tools.fed.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_fed_news_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "Federal Reserve issues FOMC statement"
        assert result["news"][0]["source"] == "Federal Reserve"


def test_get_fed_news_keyword_filter():
    with patch("alpha_agents.tools.fed.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_fed_news_fn(keyword="FOMC"))
        assert result["count"] == 1
        assert "FOMC" in result["news"][0]["title"]


def test_get_fed_news_respects_limit():
    with patch("alpha_agents.tools.fed.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_fed_news_fn(limit=2))
        assert result["count"] == 2


def test_get_fed_news_handles_fetch_error():
    with patch("alpha_agents.tools.fed.fetch", side_effect=Exception("Connection refused")):
        result = json.loads(get_fed_news_fn())
        assert result["count"] == 0
        assert result["news"] == []
