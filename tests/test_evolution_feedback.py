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


def test_inject_sentiment_handles_dict_strategy():
    """Real sentiment_cycle schema stores strategy as dict (buy_style / sell_style),
    not a plain string. Must extract buy_style rather than stringify the dict."""
    fake_cycle = {
        "phase": "升温",
        "confidence": 0.8,
        "strategy": {
            "en": "warming",
            "max_exposure_pct": 60,
            "buy_style": "可追强势，高beta优先",
            "sell_style": "放宽移动止损",
        },
    }
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert "升温" in result
    assert "可追强势" in result
    # Must not leak raw dict literals into the rendered prompt
    assert "max_exposure_pct" not in result
    assert "{'en'" not in result


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


def test_inject_principles_formats_by_category():
    fake = [
        {"id": 1, "principle": "高位缩量新高长上影 = 派发",
         "pattern_description": "...", "category": "vpa_signal",
         "action_guidance": "减仓", "win_rate": 0.8, "evidence_count": 5,
         "status": "active"},
        {"id": 2, "principle": "板块分化 = 主线虚胖",
         "pattern_description": "...", "category": "theme_timing",
         "action_guidance": "降级", "win_rate": 0.3, "evidence_count": 3,
         "status": "active"},
    ]
    with patch("alpha_agents.evolution.feedback.get_all_principles_including_weakened",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_principles
        result = inject_principles()
    assert "【交易经验手册】" in result
    assert "高位缩量新高" in result
    assert "板块分化" in result


def test_inject_principles_empty_returns_empty():
    with patch("alpha_agents.evolution.feedback.get_all_principles_including_weakened",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_principles
        assert inject_principles() == ""


def test_inject_principles_marks_weakened():
    fake = [{"id": 1, "principle": "失效法则", "pattern_description": "x",
             "category": "insight", "action_guidance": "谨慎",
             "win_rate": 0.25, "evidence_count": 4, "status": "weakened"}]
    with patch("alpha_agents.evolution.feedback.get_all_principles_including_weakened",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_principles
        result = inject_principles()
    assert "⚠️" in result


def test_inject_recent_lessons_formats():
    fake = [
        {"date": "2026-04-17", "lesson_type": "failure", "theme": "数据中心",
         "content": "东方国信破5日线"},
        {"date": "2026-04-16", "lesson_type": "success", "theme": "CPO",
         "content": "协创数据+9.4%"},
    ]
    with patch("alpha_agents.evolution.feedback.get_recent_daily_lessons",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_recent_lessons
        result = inject_recent_lessons()
    assert "【近期教训】" in result or "【近期经验】" in result
    assert "04-17" in result or "2026-04-17" in result
    assert "东方国信" in result
    assert "协创数据" in result


def test_inject_recent_lessons_empty():
    with patch("alpha_agents.evolution.feedback.get_recent_daily_lessons",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_recent_lessons
        assert inject_recent_lessons() == ""


def test_inject_playbooks_formats_active_and_degraded():
    fake = [
        {"name": "CPO突破+机构", "status": "active", "weight": 1.5,
         "hit_rate": 0.8, "total_trades": 10, "wins": 8, "annotation": ""},
        {"name": "数据中心追强", "status": "degraded", "weight": 0.5,
         "hit_rate": 0.3, "total_trades": 6, "wins": 2,
         "annotation": "主线资金退潮"},
    ]
    with patch("alpha_agents.evolution.feedback.get_active_or_degraded_playbooks",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_playbooks
        result = inject_playbooks()
    assert "Playbook" in result or "playbook" in result
    assert "CPO突破+机构" in result and "80%" in result
    assert "数据中心追强" in result and ("⚠️" in result or "degraded" in result.lower())
    assert "主线资金退潮" in result


def test_inject_playbooks_empty():
    with patch("alpha_agents.evolution.feedback.get_active_or_degraded_playbooks",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_playbooks
        assert inject_playbooks() == ""
