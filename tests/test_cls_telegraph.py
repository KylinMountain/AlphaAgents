import json
from unittest.mock import patch, MagicMock
import pandas as pd

from alpha_agents.sources.cls_telegraph import get_cls_telegraph_fn


def _make_sample_df():
    """Create a sample DataFrame matching akshare stock_info_global_cls() output."""
    return pd.DataFrame([
        {"标题": "央行宣布降准50个基点", "内容": "中国人民银行决定下调金融机构存款准备金率0.5个百分点。",
         "发布日期": "2026-04-02", "发布时间": "10:30:00"},
        {"标题": "贵州茅台发布年报", "内容": "贵州茅台2025年实现营业收入1800亿元，同比增长15%。",
         "发布日期": "2026-04-02", "发布时间": "09:00:00"},
        {"标题": "美联储维持利率不变", "内容": "美联储宣布维持联邦基金利率在当前水平不变。",
         "发布日期": "2026-04-01", "发布时间": "22:00:00"},
    ])


@patch("akshare.stock_info_global_cls")
def test_get_cls_telegraph_returns_json(mock_cls):
    mock_cls.return_value = _make_sample_df()
    result = json.loads(get_cls_telegraph_fn(limit=10))
    assert result["count"] == 3
    assert result["news"][0]["title"] == "央行宣布降准50个基点"
    assert result["news"][0]["source"] == "财联社电报"


@patch("akshare.stock_info_global_cls")
def test_get_cls_telegraph_keyword_filter(mock_cls):
    mock_cls.return_value = _make_sample_df()
    result = json.loads(get_cls_telegraph_fn(keyword="茅台"))
    assert result["count"] == 1
    assert "茅台" in result["news"][0]["title"]


@patch("akshare.stock_info_global_cls")
def test_get_cls_telegraph_respects_limit(mock_cls):
    mock_cls.return_value = _make_sample_df()
    result = json.loads(get_cls_telegraph_fn(limit=1))
    assert result["count"] == 1


def test_get_cls_telegraph_handles_error():
    with patch("akshare.stock_info_global_cls", side_effect=Exception("connection failed")):
        result = json.loads(get_cls_telegraph_fn())
        assert result["count"] == 0
        assert result["news"] == []
        assert "error" in result


@patch("akshare.stock_info_global_cls")
def test_get_cls_telegraph_empty_dataframe(mock_cls):
    mock_cls.return_value = pd.DataFrame()
    result = json.loads(get_cls_telegraph_fn())
    assert result["count"] == 0
    assert result["news"] == []
