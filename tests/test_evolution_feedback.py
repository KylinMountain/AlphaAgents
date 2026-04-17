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


def test_inject_vpa_signal_history_formats_confirmed_and_pending():
    fake_signals = [
        {"signal_type": "射击十字星", "signal_date": "2026-04-14",
         "direction": "偏空", "status": "confirmed",
         "resolved_by": "今日跌3.6%", "resolved_date": "2026-04-15"},
        {"signal_type": "缩量止跌", "signal_date": "2026-04-16",
         "direction": "偏多", "status": "pending",
         "resolved_by": "", "resolved_date": ""},
    ]
    with patch("alpha_agents.evolution.feedback._query_vpa_signals_for_code",
               return_value=fake_signals):
        from alpha_agents.evolution.feedback import inject_vpa_signal_history
        result = inject_vpa_signal_history("300274")
    assert "VPA信号历史" in result
    assert "射击十字星" in result and "✅" in result
    assert "缩量止跌" in result and "⏳" in result


def test_inject_vpa_signal_history_empty_code():
    from alpha_agents.evolution.feedback import inject_vpa_signal_history
    assert inject_vpa_signal_history("") == ""
    assert inject_vpa_signal_history("abc") == ""  # not 6 digits


def test_inject_vpa_signal_history_no_signals_returns_empty():
    with patch("alpha_agents.evolution.feedback._query_vpa_signals_for_code",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_vpa_signal_history
        assert inject_vpa_signal_history("300274") == ""
