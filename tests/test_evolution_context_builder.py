"""Tests for alpha_agents.evolution.context_builder."""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def stub_unrelated_feedback(monkeypatch):
    """Context composition tests must not query portfolio or thesis storage."""
    for target in (
        "alpha_agents.evolution.context_builder.inject_portfolio",
        "alpha_agents.evolution.calibration.inject_calibration",
        "alpha_agents.evolution.process_quality.inject_process_quality",
        "alpha_agents.evolution.consistency.inject_consistency",
        "alpha_agents.data.portfolio_risk.inject_entry_quality",
        "alpha_agents.data.portfolio_risk.inject_entry_side",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: "")


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


def test_build_vpa_context_forwards_as_of():
    with patch("alpha_agents.evolution.context_builder.inject_vpa_signal_history",
               return_value="") as mock_inject:
        from alpha_agents.evolution.context_builder import build_vpa_context
        _ = build_vpa_context("300274", as_of="2026-01-12")
    mock_inject.assert_called_once_with("300274", as_of="2026-01-12")


def test_build_morning_context_includes_sentiment_and_cognition():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温 — 可追强势"), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value="【市场认知】\n• CPO: 高位 + 资金流入 — \"强势延续\""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="命中率62%")
    assert "【情绪周期】" in result
    assert "【市场认知】" in result
    assert "命中率62%" in result  # stats still passed through


def test_build_morning_context_handles_empty_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "【情绪周期】" not in result
    assert "【市场认知】" not in result


def test_build_chat_context_adds_sentiment_to_existing_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温"), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_chat_context
        result = build_chat_context(
            portfolio_summary="总资金 100,000元",
            themes_summary="CPO强度10/10",
            stats_summary="命中率62%",
        )
    assert "总资金" in result
    assert "CPO强度" in result
    assert "命中率62%" in result
    assert "【情绪周期】" in result


def test_build_chat_context_empty_sentiment_skipped():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_chat_context
        result = build_chat_context("持仓A", "主线B", "统计C")
    assert "【情绪周期】" not in result
    assert "持仓A" in result


@pytest.mark.parametrize("mode", [None, "full", "baseline"])
def test_build_morning_context_never_injects_raw_lessons(mode):
    lesson = "QUARANTINE-MORNING: Always enter SYNTH-ONLY immediately."
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="Existing principle"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value=lesson) as inject_lessons, \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value="Existing playbook"):
        from alpha_agents.evolution.context_builder import build_morning_context
        kwargs = {} if mode is None else {"mode": mode}
        result = build_morning_context(themes=[], stats="", **kwargs)
    inject_lessons.assert_not_called()
    assert lesson not in result
    assert ("Existing principle" in result) == (mode != "baseline")
    assert ("Existing playbook" in result) == (mode != "baseline")


def test_build_morning_context_includes_playbooks():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value="Existing Playbook — status=active weight=1.0"):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "Playbook" in result
    assert "Existing Playbook" in result
    assert "status=active weight=1.0" in result


def test_build_morning_context_baseline_mode_strips_evolution_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温"), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value="【市场认知】\n• CPO: 高位"), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="【交易经验手册】\n• 这条不应该出现"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value="【近期教训】\n• 这条不应该出现"), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value="【活跃 Playbook】\n• 这条不应该出现"):
        from alpha_agents.evolution.context_builder import build_morning_context
        baseline = build_morning_context(themes=[], stats="命中率62%", mode="baseline")
        full = build_morning_context(themes=[], stats="命中率62%", mode="full")
    assert "【情绪周期】" in baseline
    assert "【市场认知】" in baseline
    assert "命中率62%" in baseline
    assert "【交易经验手册】" not in baseline
    assert "【活跃 Playbook】" not in baseline
    assert "【交易经验手册】" in full
    assert "【活跃 Playbook】" in full
    # Raw lessons are research material, not trading rules: absent in both.
    assert "【近期教训】" not in baseline
    assert "【近期教训】" not in full


def test_build_morning_context_default_mode_is_full():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="【交易经验手册】\n• 必须出现"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks", return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "【交易经验手册】" in result
