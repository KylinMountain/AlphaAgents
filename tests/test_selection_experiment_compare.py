"""Primary metric keeps portfolio-return units through bootstrap."""

from datetime import date, timedelta

import pytest

from alpha_agents.evolution import selection_experiment_compare as C


def _days(offset):
    start = date(2026, 1, 1) + timedelta(days=offset)
    return [(start + timedelta(days=i)).isoformat() for i in range(30)]


def _window(offset, control=0.0, sector=0.1):
    return {
        "days": _days(offset),
        "control_daily_returns_pct": [control] * 30,
        "sector_daily_returns_pct": [sector] * 30,
    }


def test_compound_return_not_daily_mean():
    got = C.compound_return_pct([1.0, 1.0])
    assert got == pytest.approx(2.01)


def test_four_window_bootstrap_reports_portfolio_return_difference_pp():
    windows = [
        _window(0),
        _window(40),
        _window(80),
        _window(120),
    ]
    got = C.compare_windows(
        windows=windows,
        block_length_days=5,
        repetitions=200,
        seed=7,
        minimum_total_days=120,
        minimum_meaningful_improvement_pp=1.0,
    )

    expected = C.compound_return_pct([0.1] * 30)
    assert got["status"] == "ok"
    assert got["total_unique_days"] == 120
    assert got["mean_delta_pp"] == pytest.approx(expected, abs=1e-6)
    assert got["unit"] == "percentage_points"
    assert got["bootstrap"]["cross_window_blocks"] is False


def test_bootstrap_never_crosses_reset_window_boundaries():
    windows = [
        _window(0, control=0.0, sector=0.1),
        _window(40, control=0.0, sector=-0.1),
        _window(80, control=0.0, sector=0.1),
        _window(120, control=0.0, sector=-0.1),
    ]
    got = C.compare_windows(
        windows=windows,
        block_length_days=7,
        repetitions=100,
        seed=9,
        minimum_total_days=120,
        minimum_meaningful_improvement_pp=1.0,
    )
    assert got["bootstrap"]["block_length_days"] == 7
    assert len(got["window_delta_pp"]) == 4


def test_each_registered_window_is_diagnostic_30_days_not_a_50_day_gate():
    with pytest.raises(C.SelectionCompareError, match="exactly 30"):
        C.compare_windows(
            windows=[
                {
                    "days": _days(0)[:29],
                    "control_daily_returns_pct": [0.0] * 29,
                    "sector_daily_returns_pct": [0.0] * 29,
                },
                _window(40), _window(80), _window(120),
            ],
            block_length_days=5,
            repetitions=20,
            seed=1,
            minimum_total_days=120,
            minimum_meaningful_improvement_pp=1.0,
        )


def test_overlapping_observed_days_are_refused():
    windows = [_window(0), _window(0), _window(80), _window(120)]
    with pytest.raises(C.SelectionCompareError, match="overlap"):
        C.compare_windows(
            windows=windows,
            block_length_days=5,
            repetitions=20,
            seed=1,
            minimum_total_days=120,
            minimum_meaningful_improvement_pp=1.0,
        )


def test_threshold_is_compared_in_same_percentage_point_unit():
    windows = [
        _window(0, sector=0.01),
        _window(40, sector=0.01),
        _window(80, sector=0.01),
        _window(120, sector=0.01),
    ]
    got = C.compare_windows(
        windows=windows,
        block_length_days=5,
        repetitions=100,
        seed=2,
        minimum_total_days=120,
        minimum_meaningful_improvement_pp=5.0,
    )
    assert got["evidence"] == "inconclusive"
    assert got["minimum_meaningful_improvement_pp"] == 5.0
