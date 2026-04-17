"""Tests for alpha_agents.evolution.context_builder."""
from unittest.mock import patch


def test_build_vpa_context_composes_signal_history():
    with patch("alpha_agents.evolution.context_builder.inject_vpa_signal_history",
               return_value="【该股VPA信号历史】\n• 04-14 射击十字星(偏空) → ✅已确认"):
        from alpha_agents.evolution.context_builder import build_vpa_context
        result = build_vpa_context("300274")
    assert "VPA信号历史" in result
    assert "射击十字星" in result


def test_build_vpa_context_empty_when_no_signals():
    with patch("alpha_agents.evolution.context_builder.inject_vpa_signal_history",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_vpa_context
        result = build_vpa_context("300274")
    assert result == ""


def test_build_morning_context_includes_sentiment_and_cognition():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温 — 可追强势"), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value="【市场认知】\n• CPO: 高位 + 资金流入 — \"强势延续\""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="命中率62%")
    assert "【情绪周期】" in result
    assert "【市场认知】" in result
    assert "命中率62%" in result  # stats still passed through


def test_build_morning_context_handles_empty_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "【情绪周期】" not in result
    assert "【市场认知】" not in result
