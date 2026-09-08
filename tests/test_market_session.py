"""Which markets get analysed, and when.

The monitor loop had no session gate: a fixed-interval loop calling both
agents whenever the digest found events. News flows around the clock, so
it wrote A-share sector calls at 18:29 off a closing snapshot for a
market that would not open for fifteen hours — twenty-eight reports in
three hours, each one two agent runs of tokens.
"""

from datetime import datetime

import pytest

from alpha_agents.pipeline.market_session import (
    FUTURES, STOCK, analysis_targets, describe, futures_night_enabled,
)

TUESDAY = (2026, 9, 8)      # a trading day
SATURDAY = (2026, 9, 12)


def at(h, m, day=TUESDAY, trading=True):
    return analysis_targets(datetime(*day, h, m), is_trading_day=trading)


class TestStockSession:
    @pytest.mark.parametrize("h,m", [(9, 30), (10, 0), (11, 30),
                                     (13, 0), (14, 59), (15, 0)])
    def test_inside(self, h, m):
        assert STOCK in at(h, m)

    @pytest.mark.parametrize("h,m", [(9, 29), (12, 0), (12, 59),
                                     (15, 1), (18, 29), (23, 0)])
    def test_outside(self, h, m):
        assert STOCK not in at(h, m)

    def test_lunch_break_is_excluded(self):
        """Prices do not move, so an analysis there repeats the 11:30 one."""
        assert at(12, 15) == set()


class TestTradingDay:
    def test_weekend_analyses_nothing(self):
        assert at(10, 0, day=SATURDAY, trading=False) == set()

    def test_holiday_flag_wins_over_the_clock(self):
        """The caller holds the calendar; a weekday can still be a holiday."""
        assert at(10, 0, trading=False) == set()


class TestFuturesNight:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("FUTURES_NIGHT_SESSION", raising=False)
        assert not futures_night_enabled()
        assert at(21, 30) == set()

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_toggle_spellings(self, monkeypatch, value):
        monkeypatch.setenv("FUTURES_NIGHT_SESSION", value)
        assert futures_night_enabled()

    def test_evening_runs_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FUTURES_NIGHT_SESSION", "1")
        assert at(21, 30) == {FUTURES}
        assert at(23, 0) == {FUTURES}

    def test_small_hours_run_when_enabled(self, monkeypatch):
        """The session wraps past midnight into the next calendar day."""
        monkeypatch.setenv("FUTURES_NIGHT_SESSION", "1")
        assert at(1, 0, day=(2026, 9, 9)) == {FUTURES}

    def test_saturday_night_is_closed(self, monkeypatch):
        """Friday night runs into Saturday; Saturday night does not."""
        monkeypatch.setenv("FUTURES_NIGHT_SESSION", "1")
        assert at(22, 0, day=SATURDAY, trading=False) == set()

    def test_stock_never_joins_the_night(self, monkeypatch):
        monkeypatch.setenv("FUTURES_NIGHT_SESSION", "1")
        assert STOCK not in at(22, 0)


class TestDescribe:
    def test_empty_says_ingest_only(self):
        assert "仅摄取" in describe(set())

    def test_names_both(self):
        assert describe({STOCK, FUTURES}) == "股票 + 期货"

    def test_order_is_stable(self):
        """Same set, same phrase — a log line should not shuffle."""
        assert describe({FUTURES, STOCK}) == describe({STOCK, FUTURES})
