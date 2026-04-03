import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.eastmoney_live import (
    _parse_response,
    get_eastmoney_live_fn,
)

SAMPLE_RESPONSE = 'var ajaxResult=' + json.dumps({
    "rc": 1,
    "me": "",
    "LivesList": [
        {
            "title": "央行开展1000亿元MLF操作",
            "digest": "【央行开展1000亿元MLF操作】中国人民银行今日开展1000亿元中期借贷便利操作，利率维持不变。",
            "showtime": "2024-12-01 09:30:00",
        },
        {
            "title": "",
            "digest": "特朗普发布关税新政策声明",
            "showtime": "2024-12-01 08:00:00",
        },
        {
            "title": "",
            "digest": "",
            "showtime": "",
        },
    ]
})


def test_parse_response_normal():
    items = _parse_response(SAMPLE_RESPONSE)
    assert len(items) == 2  # empty item skipped
    assert items[0]["title"] == "央行开展1000亿元MLF操作"
    assert "东方财富7x24" in items[0]["source"]


def test_parse_response_jsonp():
    inner = json.dumps({"rc": 1, "LivesList": [{"title": "test", "digest": "d", "showtime": "now"}]})
    jsonp = f"({inner});"
    items = _parse_response(jsonp)
    assert len(items) == 1


def test_parse_response_empty():
    items = _parse_response('{"rc":1,"LivesList":[]}')
    assert items == []


def test_get_eastmoney_live_returns_json():
    with patch("alpha_agents.tools.eastmoney_live.http_client") as mock:
        resp = MagicMock()
        resp.text = SAMPLE_RESPONSE
        mock.fetch.return_value = resp

        result = json.loads(get_eastmoney_live_fn(limit=10))
        assert "news" in result
        assert len(result["news"]) == 2


def test_get_eastmoney_live_keyword_filter():
    with patch("alpha_agents.tools.eastmoney_live.http_client") as mock:
        resp = MagicMock()
        resp.text = SAMPLE_RESPONSE
        mock.fetch.return_value = resp

        result = json.loads(get_eastmoney_live_fn(keyword="央行"))
        assert len(result["news"]) == 1
        assert "央行" in result["news"][0]["title"]


def test_get_eastmoney_live_respects_limit():
    with patch("alpha_agents.tools.eastmoney_live.http_client") as mock:
        resp = MagicMock()
        resp.text = SAMPLE_RESPONSE
        mock.fetch.return_value = resp

        result = json.loads(get_eastmoney_live_fn(limit=1))
        assert len(result["news"]) == 1


def test_get_eastmoney_live_handles_error():
    with patch("alpha_agents.tools.eastmoney_live.http_client") as mock:
        mock.fetch.side_effect = Exception("network error")

        result = json.loads(get_eastmoney_live_fn())
        assert result["news"] == []
