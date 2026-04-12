"""Tests for sentiment cycle phase detection."""


def test_detect_phase_freezing():
    """冰点: limit_up < 20 for 2 days, max_board <= 2, ad_ratio < 0.5"""
    from alpha_agents.data.sentiment_cycle import detect_phase
    result = detect_phase(
        limit_up_trend=[12, 15, 18],
        broken_rates=[0.3, 0.25, 0.2],
        max_boards=[2, 1, 2],
        ad_ratios=[0.3, 0.4, 0.45],
    )
    assert result["phase"] == "冰点"


def test_detect_phase_warming():
    """升温: limit_up increasing for 2 days and > 50, broken < 15%, board 3-5, ad > 1.5"""
    from alpha_agents.data.sentiment_cycle import detect_phase
    result = detect_phase(
        limit_up_trend=[40, 55, 72],
        broken_rates=[0.1, 0.12, 0.13],
        max_boards=[3, 4, 4],
        ad_ratios=[1.5, 2.0, 2.5],
    )
    assert result["phase"] == "升温"


def test_detect_phase_frenzy():
    """狂热: limit_up > 100, broken < 10%, board >= 5, ad > 3"""
    from alpha_agents.data.sentiment_cycle import detect_phase
    result = detect_phase(
        limit_up_trend=[80, 95, 120],
        broken_rates=[0.08, 0.07, 0.06],
        max_boards=[5, 6, 7],
        ad_ratios=[3.5, 4.0, 5.0],
    )
    assert result["phase"] == "狂热"


def test_detect_phase_divergence():
    """分歧: still many limit_ups but broken rate rising > 25%"""
    from alpha_agents.data.sentiment_cycle import detect_phase
    result = detect_phase(
        limit_up_trend=[100, 90, 85],
        broken_rates=[0.15, 0.22, 0.30],
        max_boards=[6, 5, 4],
        ad_ratios=[2.0, 1.5, 1.2],
    )
    assert result["phase"] == "分歧"


def test_detect_phase_retreat():
    """退潮: limit_up decreasing for 2 days, broken > 30%, board declining, ad < 1"""
    from alpha_agents.data.sentiment_cycle import detect_phase
    result = detect_phase(
        limit_up_trend=[70, 45, 25],
        broken_rates=[0.25, 0.35, 0.40],
        max_boards=[5, 3, 2],
        ad_ratios=[1.0, 0.7, 0.5],
    )
    assert result["phase"] == "退潮"


def test_strategy_for_phase():
    """Check strategy params are returned correctly."""
    from alpha_agents.data.sentiment_cycle import get_phase_strategy
    s = get_phase_strategy("升温")
    assert s["max_exposure_pct"] == 60
    assert s["trailing_stop_pct"] == 5
    assert s["theme_exit_threshold"] == 3

    s2 = get_phase_strategy("退潮")
    assert s2["max_exposure_pct"] == 15
    assert s2["trailing_stop_pct"] == 3
    assert s2["theme_exit_threshold"] == 8
