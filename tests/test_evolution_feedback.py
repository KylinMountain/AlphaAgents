"""Tests for alpha_agents.evolution.feedback."""
from unittest.mock import patch


def test_inject_sentiment_returns_formatted_block():
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先"}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert "【情绪周期】" in result
    assert "升温" in result
    assert "可追强势" in result


def test_inject_sentiment_returns_empty_when_db_empty():
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=None):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert result == ""


def test_inject_sentiment_fits_budget():
    """Sentiment section budget is 50 chars per spec."""
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先，" + "额外说明" * 30}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert len(result) <= 80  # 50 char budget + small header overhead


def test_inject_cognition_formats_sectors():
    fake_rows = [
        {"sector": "CPO", "position": "high", "fund_trend": "inflow",
         "assessment": "强势延续"},
        {"sector": "数据中心", "position": "mid", "fund_trend": "inflow",
         "assessment": "资金驱动但缺龙头"},
    ]
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=fake_rows):
        from alpha_agents.evolution.feedback import inject_cognition
        result = inject_cognition()
    assert "【市场认知】" in result
    assert "CPO" in result and "高位" in result
    assert "数据中心" in result and "中位" in result
    assert "强势延续" in result


def test_inject_cognition_empty_when_no_data():
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_cognition
        assert inject_cognition() == ""


def test_inject_cognition_respects_budget():
    """Cognition section budget is 300 chars per spec. Older/lower-priority
    entries should be truncated when exceeding."""
    fake_rows = [
        {"sector": f"Sector{i}", "position": "high", "fund_trend": "inflow",
         "assessment": "评估内容" * 10}
        for i in range(20)
    ]
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=fake_rows):
        from alpha_agents.evolution.feedback import inject_cognition
        result = inject_cognition()
    assert len(result) <= 400  # 300 budget + header overhead
