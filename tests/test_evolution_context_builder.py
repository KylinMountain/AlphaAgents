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
