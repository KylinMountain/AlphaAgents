import json
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.sources.eastmoney import get_news_fn


def _mock_stock_news():
    return pd.DataFrame({
        "标题": ["特朗普宣布对华加征关税", "央行降准0.5个百分点", "某公司发布年报"],
        "内容": ["美国总统特朗普宣布...", "中国人民银行决定...", "某公司2025年营收..."],
        "发布时间": ["2026-04-02 08:00", "2026-04-02 07:30", "2026-04-02 07:00"],
        "文章来源": ["新浪财经", "东方财富", "同花顺"],
    })


@patch("alpha_agents.sources.eastmoney._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_returns_list(mock_fetch):
    result = get_news_fn(limit=10)
    parsed = json.loads(result)
    assert len(parsed["news"]) == 3
    assert parsed["news"][0]["title"] == "特朗普宣布对华加征关税"


@patch("alpha_agents.sources.eastmoney._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_with_keyword(mock_fetch):
    result = get_news_fn(limit=10, keyword="特朗普")
    parsed = json.loads(result)
    assert len(parsed["news"]) == 1
    assert "特朗普" in parsed["news"][0]["title"]


@patch("alpha_agents.sources.eastmoney._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_respects_limit(mock_fetch):
    result = get_news_fn(limit=2)
    parsed = json.loads(result)
    assert len(parsed["news"]) == 2
