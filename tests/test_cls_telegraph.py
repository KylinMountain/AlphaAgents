import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.cls_telegraph import get_cls_telegraph_fn, _parse_item

SAMPLE_API_RESPONSE = {
    "data": {
        "roll_data": [
            {
                "title": "央行宣布降准50个基点",
                "content": "中国人民银行决定下调金融机构存款准备金率0.5个百分点，释放长期资金约1万亿元。",
                "ctime": 1743580800,
                "subjects": [
                    {"subject_name": "央行"},
                    {"subject_name": "降准"},
                ],
            },
            {
                "title": "贵州茅台发布年报",
                "content": "贵州茅台2025年实现营业收入1800亿元，同比增长15%。",
                "ctime": 1743577200,
                "subjects": [
                    {"subject_name": "茅台"},
                    {"subject_name": "年报"},
                ],
            },
            {
                "title": "美联储维持利率不变",
                "content": "美联储宣布维持联邦基金利率在当前水平不变，符合市场预期。",
                "ctime": 1743573600,
                "subjects": [
                    {"subject_name": "美联储"},
                ],
            },
        ]
    }
}


def test_parse_item():
    raw = SAMPLE_API_RESPONSE["data"]["roll_data"][0]
    item = _parse_item(raw)
    assert item["title"] == "央行宣布降准50个基点"
    assert item["source"] == "财联社电报"
    assert item["tags"] == ["央行", "降准"]
    assert "2025" in item["time"] or "2026" in item["time"]  # depends on timezone


def test_parse_item_missing_fields():
    item = _parse_item({})
    assert item["title"] == ""
    assert item["summary"] == ""
    assert item["time"] == ""
    assert item["source"] == "财联社电报"
    assert item["tags"] == []


def test_get_cls_telegraph_returns_json():
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_API_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.cls_telegraph.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_cls_telegraph_fn(limit=10))
        assert result["count"] == 3
        assert result["news"][0]["title"] == "央行宣布降准50个基点"
        assert result["news"][0]["source"] == "财联社电报"


def test_get_cls_telegraph_keyword_filter():
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_API_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.cls_telegraph.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_cls_telegraph_fn(keyword="茅台"))
        assert result["count"] == 1
        assert "茅台" in result["news"][0]["title"]


def test_get_cls_telegraph_respects_limit():
    mock_resp = MagicMock()
    mock_resp.json.return_value = SAMPLE_API_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("alpha_agents.tools.cls_telegraph.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.return_value = mock_resp

        result = json.loads(get_cls_telegraph_fn(limit=1))
        assert result["count"] == 1


def test_get_cls_telegraph_handles_error():
    with patch("alpha_agents.tools.cls_telegraph.httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = lambda s: s
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        MockClient.return_value.get.side_effect = httpx.ConnectError("connection failed")

        result = json.loads(get_cls_telegraph_fn())
        assert result["count"] == 0
        assert result["news"] == []
        assert "error" in result


# Need httpx imported for the error test
import httpx
