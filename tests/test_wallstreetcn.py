import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.wallstreetcn import get_wallstreetcn_fn, _parse_items

SAMPLE_RESPONSE = {
    "code": 20000,
    "data": {
        "items": [
            {
                "title": "美联储维持利率不变",
                "content_text": "美联储宣布维持基准利率在当前水平不变，符合市场预期。",
                "display_time": 1743580800,
                "uri": "live-123",
            },
            {
                "title": "原油价格大幅上涨",
                "content_text": "国际油价突破90美元关口，创近期新高。",
                "display_time": 1743577200,
                "uri": "live-456",
            },
            {
                "title": "",
                "content_text": "A股三大指数集体高开，沪指涨0.5%。",
                "display_time": 1743573600,
                "uri": "live-789",
            },
        ]
    },
}


def test_parse_items():
    items = _parse_items(SAMPLE_RESPONSE)
    assert len(items) == 3
    assert items[0]["title"] == "美联储维持利率不变"
    assert items[0]["source"] == "华尔街见闻"
    assert "2025" in items[0]["time"]


def test_parse_items_empty_title_uses_summary():
    items = _parse_items(SAMPLE_RESPONSE)
    assert items[2]["title"] == "A股三大指数集体高开，沪指涨0.5%。"


def test_parse_items_empty_data():
    items = _parse_items({})
    assert items == []


def test_parse_items_missing_display_time():
    data = {
        "data": {
            "items": [
                {
                    "title": "Test",
                    "content_text": "Content",
                    "display_time": None,
                    "uri": "live-1",
                }
            ]
        }
    }
    items = _parse_items(data)
    assert len(items) == 1
    assert items[0]["time"] == ""


def _mock_fetch(response_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json
    return mock_resp


def test_get_wallstreetcn_returns_json():
    with patch("alpha_agents.tools.wallstreetcn.fetch", return_value=_mock_fetch(SAMPLE_RESPONSE)):
        result = json.loads(get_wallstreetcn_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "美联储维持利率不变"


def test_get_wallstreetcn_keyword_filter():
    with patch("alpha_agents.tools.wallstreetcn.fetch", return_value=_mock_fetch(SAMPLE_RESPONSE)):
        result = json.loads(get_wallstreetcn_fn(keyword="美联储"))
        assert result["count"] == 1
        assert "美联储" in result["news"][0]["title"]


def test_get_wallstreetcn_respects_limit():
    with patch("alpha_agents.tools.wallstreetcn.fetch", return_value=_mock_fetch(SAMPLE_RESPONSE)):
        result = json.loads(get_wallstreetcn_fn(limit=1))
        assert result["count"] == 1


def test_get_wallstreetcn_handles_api_error():
    with patch("alpha_agents.tools.wallstreetcn.fetch", side_effect=Exception("500 error")):
        result = json.loads(get_wallstreetcn_fn())
        assert result["count"] == 0
        assert result["news"] == []
