import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.xinhua import get_xinhua_fn, _parse_rss, _parse_fallback_html

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>新华网财经</title>
  <item>
    <title>央行宣布降准0.5个百分点</title>
    <description>中国人民银行决定下调金融机构存款准备金率0.5个百分点，释放长期资金约1万亿元。</description>
    <pubDate>Thu, 02 Apr 2026 10:00:00 GMT</pubDate>
    <link>http://www.news.cn/fortune/2026-04/02/c_1234567.htm</link>
  </item>
  <item>
    <title>A股三大指数集体收涨</title>
    <description>沪指收涨1.2%，深成指涨1.5%，创业板指涨1.8%。</description>
    <pubDate>Thu, 02 Apr 2026 08:00:00 GMT</pubDate>
    <link>http://www.news.cn/fortune/2026-04/02/c_1234568.htm</link>
  </item>
  <item>
    <title>国务院发布稳外贸新举措</title>
    <description>国务院办公厅印发关于推动外贸稳规模优结构的意见。</description>
    <pubDate>Wed, 01 Apr 2026 14:00:00 GMT</pubDate>
    <link>http://www.news.cn/fortune/2026-04/01/c_1234569.htm</link>
  </item>
</channel>
</rss>"""

SAMPLE_HTML = """
<html><body>
<div class="news-list">
  <a href="http://www.news.cn/fortune/2026-04/02/c_001.htm">经济数据超预期增长</a>
  <a href="http://www.news.cn/fortune/2026-04/02/c_002.htm">新能源产业投资加速</a>
  <a href="http://www.news.cn/fortune/2026-04/02/c_003.htm">人民币汇率保持稳定</a>
</div>
</body></html>
"""


def test_parse_rss():
    items = _parse_rss(SAMPLE_RSS)
    assert len(items) == 3
    assert items[0]["title"] == "央行宣布降准0.5个百分点"
    assert items[0]["source"] == "新华社"
    assert "c_1234567" in items[0]["link"]


def test_parse_rss_invalid_xml():
    items = _parse_rss("not xml at all")
    assert items == []


def test_parse_rss_empty_feed():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>"""
    items = _parse_rss(xml)
    assert items == []


def test_parse_fallback_html():
    items = _parse_fallback_html(SAMPLE_HTML)
    assert len(items) == 3
    assert items[0]["source"] == "新华社"
    assert items[0]["link"].startswith("http://www.news.cn/")


def test_parse_fallback_html_deduplicates():
    html = """
    <a href="http://www.news.cn/fortune/a.htm">重复标题测试内容</a>
    <a href="http://www.news.cn/fortune/b.htm">重复标题测试内容</a>
    """
    items = _parse_fallback_html(html)
    assert len(items) == 1


def _mock_fetch(text):
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def test_get_xinhua_returns_json():
    with patch("alpha_agents.tools.xinhua.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_xinhua_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "央行宣布降准0.5个百分点"
        assert result["news"][0]["source"] == "新华社"


def test_get_xinhua_keyword_filter():
    with patch("alpha_agents.tools.xinhua.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_xinhua_fn(keyword="央行"))
        assert result["count"] == 1
        assert "央行" in result["news"][0]["title"]


def test_get_xinhua_respects_limit():
    with patch("alpha_agents.tools.xinhua.fetch", return_value=_mock_fetch(SAMPLE_RSS)):
        result = json.loads(get_xinhua_fn(limit=1))
        assert result["count"] == 1


def test_get_xinhua_handles_fetch_error():
    with patch("alpha_agents.tools.xinhua.fetch", side_effect=Exception("Connection refused")):
        result = json.loads(get_xinhua_fn())
        assert result["count"] == 0
        assert result["news"] == []


def test_get_xinhua_falls_back_to_html():
    call_count = 0

    def mock_fetch_side_effect(url):
        nonlocal call_count
        call_count += 1
        if "xml" in url:
            raise Exception("RSS unavailable")
        return _mock_fetch(SAMPLE_HTML)

    with patch("alpha_agents.tools.xinhua.fetch", side_effect=mock_fetch_side_effect):
        result = json.loads(get_xinhua_fn())
        assert result["count"] == 3
        assert result["news"][0]["source"] == "新华社"
