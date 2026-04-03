import json
from unittest.mock import patch, MagicMock

from alpha_agents.sources.pboc import get_pboc_news_fn, _parse_pboc_html

SAMPLE_HTML = """
<html>
<body>
<div class="newslist">
  <ul>
    <li>
      <a href="/goutongjiaoliu/113456/113469/5123456/index.html" title="中国人民银行决定下调存款准备金率">中国人民银行决定下调存款准备金率</a>
      <span class="date">2026-04-01</span>
    </li>
    <li>
      <a href="/goutongjiaoliu/113456/113469/5123457/index.html" title="2026年第一季度货币政策执行报告">2026年第一季度货币政策执行报告</a>
      <span class="date">2026-03-28</span>
    </li>
    <li>
      <a href="/goutongjiaoliu/113456/113469/5123458/index.html" title="关于优化跨境人民币业务的通知">关于优化跨境人民币业务的通知</a>
      <span class="date">2026-03-25</span>
    </li>
  </ul>
</div>
</body>
</html>
"""


def test_parse_pboc_html():
    items = _parse_pboc_html(SAMPLE_HTML)
    assert len(items) == 3
    assert items[0]["title"] == "中国人民银行决定下调存款准备金率"
    assert items[0]["source"] == "中国人民银行"
    assert items[0]["time"] == "2026-04-01"
    assert items[0]["link"].startswith("http://www.pbc.gov.cn/")


def test_parse_pboc_html_empty():
    items = _parse_pboc_html("<html><body>No news here</body></html>")
    assert items == []


def test_parse_pboc_html_full_urls():
    html = """
    <ul>
      <li>
        <a href="http://www.pbc.gov.cn/some/page.html" title="测试新闻标题">测试新闻标题</a>
        <span>2026-03-20</span>
      </li>
    </ul>
    """
    items = _parse_pboc_html(html)
    assert len(items) == 1
    assert items[0]["link"] == "http://www.pbc.gov.cn/some/page.html"


def _mock_fetch(text):
    mock_resp = MagicMock()
    mock_resp.text = text
    mock_resp.content = text.encode("utf-8")
    mock_resp.encoding = "utf-8"
    mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
    return mock_resp


def test_get_pboc_news_returns_json():
    with patch("alpha_agents.sources.pboc.http_fetch", return_value=_mock_fetch(SAMPLE_HTML)):
        result = json.loads(get_pboc_news_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["source"] == "中国人民银行"


def test_get_pboc_news_keyword_filter():
    with patch("alpha_agents.sources.pboc.http_fetch", return_value=_mock_fetch(SAMPLE_HTML)):
        result = json.loads(get_pboc_news_fn(keyword="准备金"))
        assert result["count"] == 1
        assert "准备金" in result["news"][0]["title"]


def test_get_pboc_news_respects_limit():
    with patch("alpha_agents.sources.pboc.http_fetch", return_value=_mock_fetch(SAMPLE_HTML)):
        result = json.loads(get_pboc_news_fn(limit=1))
        assert result["count"] == 1


def test_get_pboc_news_handles_fetch_error():
    with patch("alpha_agents.sources.pboc.http_fetch", side_effect=Exception("Connection refused")):
        result = json.loads(get_pboc_news_fn())
        assert result["count"] == 0
        assert result["news"] == []
