"""RP-02: one performance definition starts at the pre-trade account mark."""

import math

import pytest

from alpha_agents.evolution import performance as P


def _row(day, equity):
    return {"date": day, "equity": equity}


def test_initial_100_to_95_to_100_is_flat_with_five_pct_drawdown():
    got = P.equity_metrics(
        100.0,
        [_row("2026-01-05", 95.0), _row("2026-01-06", 100.0)],
    )

    assert got["trading_days"] == 2
    assert got["net_return_pct"] == pytest.approx(0.0, abs=1e-12)
    assert got["max_drawdown_pct"] == pytest.approx(5.0, abs=1e-12)
    assert got["daily_returns_pct"]["2026-01-05"] == pytest.approx(
        -5.0, abs=1e-12)
    assert got["daily_returns_pct"]["2026-01-06"] == pytest.approx(
        5.263157894736842, abs=1e-12)


def test_one_trading_day_uses_initial_mark_as_its_denominator():
    got = P.equity_metrics(100.0, [_row("2026-01-05", 99.0)])
    assert got["trading_days"] == 1
    assert got["net_return_pct"] == pytest.approx(-1.0)
    assert got["max_drawdown_pct"] == pytest.approx(1.0)
    assert got["worst_day_return_pct"] == pytest.approx(-1.0)


@pytest.mark.parametrize(
    "initial", [None, "", 0, -1, math.nan, math.inf, -math.inf])
def test_invalid_initial_equity_is_refused(initial):
    with pytest.raises(P.PerformanceError):
        P.equity_metrics(initial, [_row("2026-01-05", 100.0)])


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "empty"),
        ([_row("2026-01-05", None)], "missing"),
        ([_row("2026-01-05", math.nan)], "finite"),
        ([_row("2026-01-05", math.inf)], "finite"),
        ([_row("2026-01-05", -1)], ">= 0"),
        ([_row("2026/01/05", 100)], "invalid ISO date"),
        (
            [_row("2026-01-05", 100), _row("2026-01-05", 101)],
            "duplicate",
        ),
        (
            [_row("2026-01-06", 100), _row("2026-01-05", 101)],
            "out-of-order",
        ),
    ],
)
def test_invalid_equity_history_is_refused(rows, message):
    with pytest.raises(P.PerformanceError, match=message):
        P.equity_metrics(100.0, rows)


def test_zero_equity_can_be_a_terminal_total_loss_but_not_a_new_denominator():
    got = P.equity_metrics(100.0, [_row("2026-01-05", 0.0)])
    assert got["net_return_pct"] == pytest.approx(-100.0)
    assert got["max_drawdown_pct"] == pytest.approx(100.0)

    with pytest.raises(P.PerformanceError, match="non-positive equity"):
        P.equity_metrics(
            100.0,
            [_row("2026-01-05", 0.0), _row("2026-01-06", 1.0)],
        )
