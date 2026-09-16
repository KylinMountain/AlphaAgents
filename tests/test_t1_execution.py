"""A daily bar settles less than a backtest would like, and this pins that.

Two of the review's twelve required tests live here:

* **No same-bar fantasy** — when one session touches both an entry and a stop, no
  optimistic path may be produced.
* **No future volume** — the fill decision at the open must not read the session
  it is filling in.

The second is enforced structurally rather than by discipline: ``DayBar`` has no
volume field, so the look-ahead has no way to be spelled. One test below asserts
that absence, deliberately — if a future change adds the field to make something
convenient, it should fail here and force the conversation rather than pass
quietly.
"""

import dataclasses
from dataclasses import fields

import pytest

from alpha_agents.data.t1_execution import (
    AMBIGUOUS, FILLED, NO_FILL, DayBar, average_daily_volume, capacity_shares,
    limit_on_open, market_on_open, two_levels,
)

#: The review's own example. Entry 10.00 and stop 9.50 both inside [9.40, 10.50].
REVIEW_BAR = DayBar(date="2020-06-02", open=10.30, high=10.50, low=9.40,
                    close=10.20)


class TestTheDayBarCannotCarryTheFuture:
    def test_it_has_no_volume_field(self):
        """Capacity must come from ADV20, so the fill day's volume is unspellable."""
        assert {f.name for f in fields(DayBar)} == {
            "date", "open", "high", "low", "close"}

    def test_capacity_reads_adv20_and_has_no_other_input(self):
        params = set(
            __import__("inspect").signature(capacity_shares).parameters)
        assert "adv20" in params
        assert not {"volume", "bar", "day", "today"} & params, \
            "a name that could carry the fill day's own volume got in"

    def test_a_short_history_gets_none_rather_than_a_proxy(self):
        """Nineteen sessions is not twenty; the caller asked for ADV20."""
        assert average_daily_volume([100.0] * 19, window=20) is None
        assert average_daily_volume([100.0] * 20, window=20) == 100.0

    def test_it_averages_only_the_window_before_the_fill(self):
        volumes = [1.0] * 25 + [9_999.0]   # the last value stands for the fill day
        assert average_daily_volume(volumes[:-1], window=20) == 1.0


class TestNoSameBarFantasy:
    def test_both_levels_touched_is_ambiguous_not_a_round_trip(self):
        """The exact case the review named. Any answer here would be invented."""
        got = two_levels(REVIEW_BAR, lower=9.50, upper=10.00)
        assert got.status == AMBIGUOUS
        assert got.price is None, "an ambiguous session must not name a price"
        assert "does not order them" in got.reason

    def test_only_the_upper_level_is_a_fill(self):
        bar = DayBar(date="2020-06-02", open=9.80, high=10.50, low=9.70, close=10.20)
        got = two_levels(bar, lower=9.50, upper=10.00)
        assert (got.status, got.price) == (FILLED, 10.00)

    def test_only_the_lower_level_is_a_fill(self):
        # high stays under the target, so only the stop was reached.
        bar = DayBar(date="2020-06-02", open=9.80, high=9.95, low=9.40, close=9.60)
        got = two_levels(bar, lower=9.50, upper=10.00)
        assert (got.status, got.price) == (FILLED, 9.50)

    def test_neither_level_is_a_no_fill(self):
        bar = DayBar(date="2020-06-02", open=9.90, high=9.95, low=9.60, close=9.80)
        got = two_levels(bar, lower=9.50, upper=10.00)
        assert got.status == NO_FILL

    def test_the_levels_must_be_ordered(self):
        with pytest.raises(ValueError, match="must be below"):
            two_levels(REVIEW_BAR, lower=10.00, upper=9.50)


class TestMarketOnOpen:
    def test_it_fills_at_the_open(self):
        got = market_on_open(REVIEW_BAR, side="buy")
        assert (got.status, got.price) == (FILLED, 10.30)

    def test_an_open_at_the_up_limit_is_not_a_buy(self):
        """The most expensive thing an A-share backtest gets wrong."""
        got = market_on_open(
            DayBar(date="2020-06-02", open=11.00, high=11.00, low=11.00, close=11.00),
            side="buy", prev_close=10.0, limit_pct=0.10, limit_rule="主板 ±10%")
        assert got.status == NO_FILL
        assert "up limit" in got.reason and "主板 ±10%" in got.reason

    def test_an_open_at_the_down_limit_is_not_a_sell(self):
        got = market_on_open(
            DayBar(date="2020-06-02", open=9.00, high=9.00, low=9.00, close=9.00),
            side="sell", prev_close=10.0, limit_pct=0.10)
        assert got.status == NO_FILL
        assert "down limit" in got.reason

    def test_a_limit_sell_above_the_cap_still_fills(self):
        """Only the direction without a counterparty is refused."""
        got = market_on_open(
            DayBar(date="2020-06-02", open=11.00, high=11.00, low=11.00, close=11.00),
            side="sell", prev_close=10.0, limit_pct=0.10)
        assert got.status == FILLED

    def test_the_rule_needs_both_numbers_or_it_is_not_applied(self):
        """A prev_close without a percentage is not a limit; do not invent one."""
        got = market_on_open(
            DayBar(date="2020-06-02", open=11.00, high=11.00, low=11.00, close=11.00),
            side="buy", prev_close=10.0)
        assert got.status == FILLED

    def test_a_bad_side_is_refused(self):
        with pytest.raises(ValueError, match="side must be"):
            market_on_open(REVIEW_BAR, side="hold")


class TestLimitAgainstTheOpen:
    def test_a_buy_limit_above_the_open_fills_at_the_open(self):
        """A resting bid at or above the opening price is satisfied by the auction."""
        got = limit_on_open(REVIEW_BAR, 10.50, side="buy")
        assert (got.status, got.price) == (FILLED, 10.30)

    def test_a_buy_limit_reached_by_the_low_fills_at_the_limit(self):
        got = limit_on_open(REVIEW_BAR, 10.00, side="buy")
        assert (got.status, got.price) == (FILLED, 10.00)

    def test_a_buy_limit_the_low_never_reached_does_not_fill(self):
        got = limit_on_open(REVIEW_BAR, 9.00, side="buy")
        assert got.status == NO_FILL
        assert "never reached" in got.reason

    def test_a_sell_limit_below_the_open_fills_at_the_open(self):
        got = limit_on_open(REVIEW_BAR, 9.50, side="sell")
        assert (got.status, got.price) == (FILLED, 10.30)

    def test_a_sell_limit_reached_by_the_high_fills_at_the_limit(self):
        got = limit_on_open(REVIEW_BAR, 10.50, side="sell")
        assert (got.status, got.price) == (FILLED, 10.50)

    def test_a_sell_limit_the_high_never_reached_does_not_fill(self):
        got = limit_on_open(REVIEW_BAR, 11.00, side="sell")
        assert got.status == NO_FILL

    def test_a_non_positive_limit_is_refused(self):
        with pytest.raises(ValueError, match="must be positive"):
            limit_on_open(REVIEW_BAR, 0.0, side="buy")


class TestCapacity:
    def test_it_floors_to_whole_lots(self):
        assert capacity_shares(1_001_000.0) == 100_100  # 10% = 100,100, already a lot
        assert capacity_shares(1_009_999.0) == 100_900  # floors down to the lot

    def test_no_history_means_no_capacity_rather_than_a_default(self):
        assert capacity_shares(None) == 0
        assert capacity_shares(0.0) == 0

    def test_the_participation_assumption_is_explicit(self):
        assert capacity_shares(1_000_000.0, participation=0.05) == 50_000
        with pytest.raises(ValueError, match="participation"):
            capacity_shares(1_000_000.0, participation=1.5)

    def test_the_whole_chain_is_adv20_to_lots(self):
        """What the walk will actually call: prior volumes in, tradable lots out."""
        prior = [1_000_000.0] * 20
        adv20 = average_daily_volume(prior)
        assert adv20 == 1_000_000.0
        assert capacity_shares(adv20) == 100_000


def test_the_module_exposes_no_way_to_pass_a_fill_day_volume():
    """A last structural sweep: no public function takes a volume for the fill day."""
    import inspect
    from alpha_agents.data import t1_execution as mod

    for name, obj in vars(mod).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != mod.__name__:
            continue
        params = set(inspect.signature(obj).parameters)
        assert "volume" not in params or name == "average_daily_volume", (
            f"{name} takes a volume; only the ADV helper may, and only for the "
            "sessions before the fill")

    assert dataclasses.fields(DayBar) is not None  # the type is a dataclass
