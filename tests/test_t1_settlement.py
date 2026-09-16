"""What one daily bar settles about a T+1 order, and what it refuses to.

Written **in pairs**. Every rule that refuses has a companion that must succeed,
because a suite made only of refusals passes just as well when the code refuses
everything. Three of these are the review's own required tests:

* **no same-bar fantasy** — a session that touched the order but not at the open
  is recorded ``intraday_ambiguous``, never resolved into a fill;
* **no future volume** — enforced structurally in ``t1_execution`` and asserted
  there, not repeated here;
* **the rule change of 2020-08-24** — the same bar settles differently on the two
  sides of it, which is D20 reaching the settlement path.

And one is a source guard: ``check_pending_orders`` must call the shared
zone predicate rather than carry its own copy. Static guards in this repository
have repeatedly proved self-satisfying, so that one is written against the
function *body* and is backed by a mutation probe recorded in the exec plan.
"""

import ast
import inspect
from pathlib import Path

import pytest

from alpha_agents.data import t1_settlement as S
from alpha_agents.data.market_rules import market_rules
from alpha_agents.data.t1_execution import DayBar

#: A main-board name, ±10%.
MAIN = "600000"
#: A ChiNext name, ±10% before 2020-08-24 and ±20% from it.
CHINEXT = "300750"
#: The two sides of the reform.
BEFORE_REFORM = "2020-08-21"
REFORM_DAY = "2020-08-24"

PREV_CLOSE = 10.00


def bar(open_: float, high: float, low: float, close: float,
        date: str = "2025-07-01") -> DayBar:
    return DayBar(date=date, open=open_, high=high, low=low, close=close)


def plain_rule(code: str = MAIN, date: str = "2025-07-01"):
    return market_rules(code, date, name="测试股")


def entry(*, entry_low=None, entry_high=None, bar_=None, prev_close=PREV_CLOSE,
          rule=None, code=MAIN):
    return S.entry_verdict(
        entry_low=entry_low, entry_high=entry_high, bar=bar_,
        prev_close=prev_close,
        rule=plain_rule(code) if rule is None else rule)


class TestTheOpenIsWhatDecides:
    def test_the_open_inside_the_zone_fills_at_the_open(self):
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.00, 10.60, 9.30, 10.40))
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 10.00, "a fill must be the open, not a round number"

    def test_an_upper_bound_alone_fills_at_or_below_it(self):
        got = entry(entry_high=10.20, bar_=bar(10.00, 10.60, 9.30, 10.40))
        assert got.status == S.FILLED_AT_OPEN

    def test_a_lower_bound_alone_is_a_breakout_and_fills_above_it(self):
        got = entry(entry_low=9.80, bar_=bar(10.00, 10.60, 9.30, 10.40))
        assert got.status == S.FILLED_AT_OPEN

    def test_no_zone_at_all_is_a_market_order(self):
        got = entry(bar_=bar(10.00, 10.60, 9.30, 10.40))
        assert got.status == S.FILLED_AT_OPEN

    def test_an_open_above_the_zone_and_a_range_short_of_it_does_not_fill(self):
        """The companion to the first test: refusing is not the default answer."""
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.30, 10.60, 10.25, 10.40))
        assert got.status == S.NO_FILL
        assert got.price is None


class TestATouchTheOpenMissedIsRecordedNotResolved:
    """The rule this module is narrower than ``limit_on_open`` on, on purpose."""

    def test_the_low_reaching_the_zone_is_ambiguous_not_a_fill(self):
        """``limit_on_open`` would fill this at 10.20. M1 declines to.

        The level is a price the bar cannot prove the order was resting before,
        and the order also carries a stop — one bar does not order two levels.
        If someone "fixes" this into a fill, this test is the conversation.
        """
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.30, 10.60, 10.05, 10.40))
        assert got.status == S.INTRADAY_AMBIGUOUS, (
            "the session dipped into the zone but opened above it — that is a "
            "path question, and M1 has no path")
        assert got.price is None, "an ambiguous session must not name a price"

    def test_the_high_reaching_a_breakout_zone_is_ambiguous_not_a_fill(self):
        got = entry(entry_low=10.50, bar_=bar(10.10, 10.60, 10.00, 10.40))
        assert got.status == S.INTRADAY_AMBIGUOUS
        assert got.price is None

    def test_a_bar_that_does_nothing_at_all_is_no_fill_not_ambiguous(self):
        """The companion: ambiguity must mean something actually happened.

        The open is kept clear of the +10% limit on purpose — a scenario that
        lands on it would answer ``limit_blocked`` and quietly stop being a
        ``no_fill`` case (which is how the vocabulary test below caught it).
        """
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.80, 11.20, 10.60, 11.00))
        assert got.status == S.NO_FILL

    def test_the_status_string_is_the_plans_own_word(self):
        """The report's counter and the code's vocabulary cannot drift apart."""
        assert S.INTRADAY_AMBIGUOUS == "intraday_ambiguous"


class TestOneWayLimitsHaveNoCounterparty:
    def test_an_open_at_the_up_limit_does_not_fill(self):
        """§5.2, and the single most expensive thing a backtest gets wrong."""
        got = entry(entry_low=10.00, entry_high=11.00,
                    bar_=bar(11.00, 11.00, 11.00, 11.00))
        assert got.status == S.LIMIT_BLOCKED
        assert got.price is None
        assert "no counterparty" in got.reason

    def test_an_open_one_cent_below_the_up_limit_fills(self):
        """The companion, and it is the whole point: 一字板 is exact, not fuzzy."""
        got = entry(entry_low=10.00, entry_high=11.00,
                    bar_=bar(10.99, 10.99, 10.50, 10.80))
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 10.99

    def test_the_main_board_limit_is_ten_percent(self):
        got = entry(entry_low=10.00, entry_high=12.00, code=MAIN,
                    bar_=bar(11.00, 11.00, 11.00, 11.00))
        assert got.status == S.LIMIT_BLOCKED

    def test_the_same_bar_settles_differently_across_the_chinext_reform(self):
        """D20, reaching the settlement path.

        On 2020-08-21 a ChiNext name is capped at ±10%, so an open at exactly
        +10% is 一字板 and a buy has no counterparty. On 2020-08-24 the cap is
        ±20%, the same open is an ordinary print, and the same order fills.
        A settlement path that read the board from the code prefix instead of
        the date would answer the same on both days.
        """
        locked = bar(11.00, 11.00, 11.00, 11.00, date=BEFORE_REFORM)
        got_before = entry(entry_low=10.00, entry_high=12.00, code=CHINEXT,
                           bar_=locked, rule=market_rules(CHINEXT, BEFORE_REFORM))
        assert got_before.status == S.LIMIT_BLOCKED

        free = bar(11.00, 11.20, 10.80, 11.00, date=REFORM_DAY)
        got_after = entry(entry_low=10.00, entry_high=12.00, code=CHINEXT,
                          bar_=free, rule=market_rules(CHINEXT, REFORM_DAY))
        assert got_after.status == S.FILLED_AT_OPEN

    def test_a_down_limit_does_not_sell(self):
        got = S.exit_verdict(bar=bar(9.00, 9.00, 9.00, 9.00),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.LIMIT_BLOCKED


class TestSuspensionAndSilence:
    def test_no_bar_is_suspended(self):
        got = entry(entry_low=9.50, entry_high=10.20, bar_=None)
        assert got.status == S.SUSPENDED

    def test_a_suspended_order_is_still_waiting_not_cancelled(self):
        """It is neither filled nor pulled: the instrument did not trade."""
        got = entry(entry_low=9.50, entry_high=10.20, bar_=None)
        assert got.status in S.STILL_WAITING

    def test_a_missing_rule_produces_no_fill(self):
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.00, 10.60, 9.30, 10.40),
                    rule=object(), prev_close=None)
        assert got.status == S.UNDECIDABLE
        assert got.price is None

    def test_a_missing_previous_close_produces_no_fill(self):
        """Without prev_close there is no limit, and no limit is not a licence."""
        got = entry(entry_low=9.50, entry_high=10.20,
                    bar_=bar(10.00, 10.60, 9.30, 10.40), prev_close=None)
        assert got.status == S.UNDECIDABLE

    def test_an_uncapped_listing_still_settles(self):
        """The companion that keeps ``UNDECIDABLE`` from swallowing a real case.

        A ChiNext listing's first five sessions are genuinely uncapped, so
        ``price_limit_pct is None`` there means "there is no limit to check" —
        not "the limit could not be determined". Conflating the two would make
        every new listing unsettleable.
        """
        rule = market_rules(CHINEXT, "2025-07-01", listed_trading_days=2)
        assert rule.price_limit_pct is None
        got = entry(entry_low=9.50, entry_high=10.20, code=CHINEXT, rule=rule,
                    bar_=bar(10.00, 20.00, 5.00, 15.00))
        assert got.status == S.FILLED_AT_OPEN


class TestExitsSettleOrRefuse:
    def test_only_the_stop_touched_fills_at_the_stop(self):
        got = S.exit_verdict(bar=bar(10.50, 10.60, 9.40, 9.50),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 9.50

    def test_only_the_target_touched_fills_at_the_target(self):
        got = S.exit_verdict(bar=bar(10.50, 12.10, 10.40, 12.00),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 12.00

    def test_both_touched_is_ambiguous(self):
        """The review's own example, now on the exit side."""
        got = S.exit_verdict(bar=bar(10.30, 12.50, 9.40, 11.00),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.INTRADAY_AMBIGUOUS
        assert got.price is None

    def test_neither_touched_holds(self):
        got = S.exit_verdict(bar=bar(10.50, 11.00, 10.20, 10.80),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.NO_FILL

    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        """The anti-optimism case: the stop is a market order once triggered.

        The open is kept above the −10% down limit (9.00) on purpose: an open
        sitting *on* the limit has no counterparty for a sell and answers
        ``limit_blocked``, which is a different (and also correct) refusal.
        """
        got = S.exit_verdict(bar=bar(9.10, 9.20, 9.00, 9.05),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.30, target_price=12.00)
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 9.10, (
            "filling at the 9.30 stop would be 2.2% better than the market "
            "gave; the plan's whole stance is to under-simulate rather than "
            "invent")

    def test_a_gap_through_the_target_fills_at_the_open(self):
        """A resting sell limit fills at the better of the two."""
        got = S.exit_verdict(bar=bar(12.50, 12.80, 12.40, 12.60),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 12.50

    def test_a_stop_only_position_settles(self):
        got = S.exit_verdict(bar=bar(10.50, 10.60, 9.40, 9.60),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=None)
        assert got.status == S.FILLED_AT_OPEN
        assert got.price == 9.50

    def test_a_position_with_no_levels_does_not_settle(self):
        got = S.exit_verdict(bar=bar(10.50, 10.60, 9.40, 9.60),
                             prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=None, target_price=None)
        assert got.status == S.NO_FILL

    def test_a_suspended_position_is_not_sold(self):
        got = S.exit_verdict(bar=None, prev_close=PREV_CLOSE, rule=plain_rule(),
                             stop_loss=9.50, target_price=12.00)
        assert got.status == S.SUSPENDED


class TestTPlusOne:
    def test_shares_bought_today_may_not_be_sold_today(self):
        assert S.may_sell("2025-07-01", "2025-07-01") is False

    def test_the_next_session_they_may(self):
        assert S.may_sell("2025-07-01", "2025-07-02") is True

    def test_a_position_with_no_open_date_may_not_be_sold(self):
        """Missing must not be the permissive case — the gate's own discipline."""
        assert S.may_sell(None, "2025-07-02") is False
        assert S.may_sell("", "2025-07-02") is False

    def test_a_timestamp_is_read_as_its_date(self):
        assert S.may_sell("2025-07-01 09:30", "2025-07-02") is True


class TestTheVocabulary:
    """A report iterates the statuses; they must not drift from the code."""

    SCENARIOS = (
        dict(entry_low=9.50, entry_high=10.20, bar_=bar(10.00, 10.6, 9.3, 10.4)),
        dict(entry_low=9.50, entry_high=10.20, bar_=bar(10.8, 11.2, 10.6, 11.0)),
        dict(entry_low=9.50, entry_high=10.20, bar_=bar(10.3, 10.6, 10.05, 10.4)),
        dict(entry_low=10.00, entry_high=11.00, bar_=bar(11.0, 11.0, 11.0, 11.0)),
        dict(entry_low=9.50, entry_high=10.20, bar_=None),
        dict(entry_low=9.50, entry_high=10.20, bar_=bar(10.0, 10.6, 9.3, 10.4),
             prev_close=None),
    )

    def test_every_reachable_status_is_declared(self):
        seen = {entry(**kw).status for kw in self.SCENARIOS}
        assert seen == set(S.ALL_STATUSES), (
            "the scenarios no longer reach every status, or a new status was "
            "added without a scenario — either way the report would silently "
            "miss a bucket")

    def test_only_a_fill_names_a_price(self):
        for kw in self.SCENARIOS:
            got = entry(**kw)
            assert (got.price is not None) == (got.status == S.FILLED_AT_OPEN), \
                f"{got.status} answered with price={got.price}"

    def test_still_waiting_is_everything_that_is_not_a_fill(self):
        assert set(S.STILL_WAITING) == set(S.ALL_STATUSES) - {S.FILLED_AT_OPEN}


class TestBarFromRow:
    def test_a_valid_row_becomes_a_bar(self):
        got = S.bar_from_row({"date": "2025-07-01", "open": 10.0, "high": 10.6,
                              "low": 9.3, "close": 10.4, "volume": 12345})
        assert got == bar(10.0, 10.6, 9.3, 10.4)

    def test_a_zero_open_is_not_a_bar(self):
        """A bar of zeros would price a fill at 0.00 and look like data."""
        assert S.bar_from_row({"date": "2025-07-01", "open": 0.0, "high": 10.6,
                               "low": 0.0, "close": 10.4}) is None

    def test_a_missing_field_is_not_a_bar(self):
        assert S.bar_from_row({"date": "2025-07-01", "open": 10.0}) is None

    def test_no_row_is_not_a_bar(self):
        assert S.bar_from_row({}) is None


def _func_node(func_name: str, path: Path) -> ast.FunctionDef:
    """The AST node for ``func_name`` in ``path``.

    AST rather than a regex on the source: a regex guard would be satisfied by
    the module-level import line, which is exactly the self-satisfying check
    this repository has been burned by before.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError(f"{func_name} is not defined in {path}")


def _body_calls(func_name: str, path: Path) -> set[str]:
    """Names called anywhere inside ``func_name``'s body."""
    return {n.func.id for n in ast.walk(_func_node(func_name, path))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


class TestTheZoneIsReadOnce:
    PORTFOLIO = (Path(__file__).resolve().parent.parent
                 / "alpha_agents" / "data" / "portfolio.py")

    def test_check_pending_orders_calls_the_shared_predicate(self):
        called = _body_calls("check_pending_orders", self.PORTFOLIO)
        assert "in_entry_zone" in called, (
            "check_pending_orders no longer calls the shared zone predicate — "
            "the entry rule and the settlement rule can now drift apart")

    def test_check_pending_orders_does_not_rebuild_the_zone_inline(self):
        """The import line alone must not satisfy the guard above.

        Written against the assignment to ``triggered`` rather than against
        "any comparison mentioning entry_low/entry_high": ``check_pending_orders``
        also holds a legitimate ``price > entry_high * 1.05`` runaway check, and
        a guard that fired on that one would be turned off instead of fixed.
        The trigger answer must be one assignment, and it must be the call.
        """
        func = _func_node("check_pending_orders", self.PORTFOLIO)
        assigns = [
            node for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "triggered"
                    for t in node.targets)
        ]
        assert len(assigns) == 1, (
            f"`triggered` is assigned {len(assigns)} times — an inline if/elif "
            "chain is back alongside the shared predicate")
        value = assigns[0].value
        assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name), \
            "`triggered` is no longer answered by a single call"
        assert value.func.id == "in_entry_zone", (
            f"`triggered` comes from {value.func.id}, not the shared predicate")

    @pytest.mark.parametrize("price,low,high,expected", [
        (10.00, 9.50, 10.20, True),
        (10.30, 9.50, 10.20, False),
        (10.00, None, 10.20, True),
        (10.30, None, 10.20, False),
        (10.00, 9.80, None, True),
        (9.50, 9.80, None, False),
        (10.00, None, None, True),
        (10.00, 0.0, 0.0, True),   # a zeroed zone is "no zone", not "buy at zero"
    ])
    def test_the_predicate_reads_a_zone_both_ways(self, price, low, high, expected):
        from alpha_agents.data.t1_execution import in_entry_zone
        assert in_entry_zone(price, low, high) is expected

    def test_the_predicate_is_reachable_from_the_settlement_module(self):
        """A live sanity check that the import is the same object, not a copy."""
        from alpha_agents.data.t1_execution import in_entry_zone
        assert inspect.getmodule(in_entry_zone).__name__ == (
            "alpha_agents.data.t1_execution")
        assert in_entry_zone(10.00, 9.50, 10.20) is True
