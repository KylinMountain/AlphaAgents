import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.jin10 import get_jin10_fn, _parse_item

# Sample data matching the flash_newest.js format: a flat JSON array
# wrapped in "var newest = [...];"
SAMPLE_ITEMS = [
    {
        "data": {
            "content": "中国人民银行决定下调金融机构存款准备金率0.5个百分点，释放长期资金约1万亿元。此次降准旨在支持实体经济发展。",
        },
        "time": "2026-04-02 10:30:00",
        "type": 1,
    },
    {
        "data": {
            "content": "美国3月非农就业人数增加25万人，预期增加20万人，前值修正为增加18万人。",
        },
        "time": "2026-04-02 10:15:00",
        "type": 0,
    },
    {
        "data": {
            "content": "贵州茅台午后涨超3%，白酒板块集体走强。",
        },
        "time": "2026-04-02 10:00:00",
        "type": 0,
    },
]

SAMPLE_JS_TEXT = "var newest = " + json.dumps(SAMPLE_ITEMS, ensure_ascii=False) + ";"


def test_parse_item():
    raw = SAMPLE_ITEMS[0]
    item = _parse_item(raw)
    content = SAMPLE_ITEMS[0]["data"]["content"]
    assert item["title"] == content[:50]
    assert len(item["title"]) == 50
    assert item["source"] == "金十数据"
    assert item["time"] == "2026-04-02 10:30:00"
    assert "降准" in item["summary"]


def test_parse_item_missing_fields():
    item = _parse_item({})
    assert item["title"] == ""
    assert item["summary"] == ""
    assert item["time"] == ""
    assert item["source"] == "金十数据"


def test_parse_item_short_content():
    """Content shorter than 50 chars should be used as-is for title."""
    raw = {"data": {"content": "短新闻"}, "time": "2026-04-02 09:00:00", "type": 0}
    item = _parse_item(raw)
    assert item["title"] == "短新闻"
    assert item["summary"] == "短新闻"


def _mock_fetch(js_text):
    """Mock fetch() to return a response with .text matching JS format."""
    mock_resp = MagicMock()
    mock_resp.text = js_text
    return mock_resp


def test_get_jin10_returns_json():
    with patch("alpha_agents.tools.jin10.fetch", return_value=_mock_fetch(SAMPLE_JS_TEXT)):
        result = json.loads(get_jin10_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["source"] == "金十数据"
        assert result["news"][0]["time"] == "2026-04-02 10:30:00"


def test_get_jin10_keyword_filter():
    with patch("alpha_agents.tools.jin10.fetch", return_value=_mock_fetch(SAMPLE_JS_TEXT)):
        result = json.loads(get_jin10_fn(keyword="茅台"))
        assert result["count"] == 1
        assert "茅台" in result["news"][0]["summary"]


def test_get_jin10_respects_limit():
    with patch("alpha_agents.tools.jin10.fetch", return_value=_mock_fetch(SAMPLE_JS_TEXT)):
        result = json.loads(get_jin10_fn(limit=1))
        assert result["count"] == 1


def test_get_jin10_handles_error():
    with patch("alpha_agents.tools.jin10.fetch", side_effect=Exception("connection failed")):
        result = json.loads(get_jin10_fn())
        assert result["count"] == 0
        assert result["news"] == []
        assert "error" in result


def test_get_jin10_empty_response():
    empty_js = "var newest = [];"
    with patch("alpha_agents.tools.jin10.fetch", return_value=_mock_fetch(empty_js)):
        result = json.loads(get_jin10_fn())
        assert result["count"] == 0
        assert result["news"] == []
