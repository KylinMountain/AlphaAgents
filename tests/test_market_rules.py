"""The rule that a 2020 walk-forward must obey did not hold in 2020.

ChiNext is in the default universe, and its daily limit was raised from 10% to
20% on 2020-08-24 — so a replay that starts in 2020 crosses the change and a
static per-prefix constant would be wrong on one side of it for every 300/301
name. Risk-warned ChiNext names changed on the same date (5% → 20%), which is
*not* the main board's 5% rule.

Sources are in the module docstring; the exchange's own explainer is quoted
there. The tests below pin the change, the ChiNext-vs-main difference for
risk-warned names, and — just as importantly — that what the function cannot
determine comes back named rather than defaulted.
"""

import pytest

from alpha_agents.data.market_rules import (
    CHINEXT_LIMIT, LOT_SIZE, MAIN_BOARD_LIMIT, RISK_WARNED_LIMIT,
    market_rules, normalise_code, normalise_date,
)

DAY_BEFORE_REFORM = "2020-08-21"
REFORM_DAY = "2020-08-24"


class TestTheChangeTheWalkCrosses:
    def test_the_last_session_before_the_reform_is_ten_percent(self):
        rule = market_rules("300001", DAY_BEFORE_REFORM, name="某创业板公司")
        assert rule.price_limit_pct == MAIN_BOARD_LIMIT

    def test_the_reform_day_is_twenty_percent(self):
        rule = market_rules("300001", REFORM_DAY, name="某创业板公司")
        assert rule.price_limit_pct == CHINEXT_LIMIT

    def test_the_two_dates_disagree(self):
        """The whole reason this is a service and not an `if`."""
        before = market_rules("300001", DAY_BEFORE_REFORM, name="x")
        after = market_rules("300001", REFORM_DAY, name="x")
        assert before.price_limit_pct != after.price_limit_pct

    def test_a_risk_warned_chinext_name_follows_the_board_after_the_reform(self):
        """ChiNext's risk-warned limit is 5% *before* the reform and 20% after —
        it does not inherit the main board's 5%."""
        before = market_rules("300001", DAY_BEFORE_REFORM, name="ST某公司")
        after = market_rules("300001", REFORM_DAY, name="ST某公司")
        assert before.price_limit_pct == RISK_WARNED_LIMIT
        assert after.price_limit_pct == CHINEXT_LIMIT


class TestMainBoard:
    @pytest.mark.parametrize("code", ["600000", "000001", "002415", "003816"])
    def test_ordinary_names_are_ten_percent_on_both_dates(self, code):
        for day in (DAY_BEFORE_REFORM, REFORM_DAY):
            assert market_rules(code, day, name="平安银行").price_limit_pct \
                == MAIN_BOARD_LIMIT

    def test_risk_warned_names_are_five_percent(self):
        rule = market_rules("600000", REFORM_DAY, name="*ST某公司")
        assert rule.price_limit_pct == RISK_WARNED_LIMIT

    def test_a_star_prefix_counts_as_risk_warned(self):
        assert market_rules("000001", REFORM_DAY, name="*ST某某").price_limit_pct \
            == RISK_WARNED_LIMIT


class TestLimitPrices:
    def test_bounds_are_rounded_to_the_tick(self):
        rule = market_rules("600000", REFORM_DAY, name="x")
        upper, lower = rule.limit_prices(10.0)
        assert (upper, lower) == (11.0, 9.0)

    def test_an_uncapped_day_has_no_bounds(self):
        """ChiNext's first five sessions: a number here would be a lie."""
        rule = market_rules("300001", REFORM_DAY, name="x", listed_trading_days=2)
        assert rule.price_limit_pct is None
        assert rule.limit_prices(10.0) == (None, None)


class TestItRefusesRatherThanGuesses:
    def test_a_board_outside_the_model_raises(self):
        with pytest.raises(ValueError, match="outside the modelled boards"):
            market_rules("688001", REFORM_DAY, name="x")

    def test_an_unparseable_code_raises(self):
        with pytest.raises(ValueError, match="six-digit"):
            market_rules("not-a-code", REFORM_DAY)

    def test_an_unparseable_date_raises(self):
        with pytest.raises(ValueError, match="unrecognised date"):
            market_rules("600000", "24/08/2020")

    def test_a_missing_name_is_named_as_unchecked(self):
        """Risk-warning status lives in the name, so without it the answer is
        the *unwarned* limit and the caller must be told that."""
        rule = market_rules("600000", REFORM_DAY)
        assert "st_status" in rule.unchecked
        assert market_rules("600000", REFORM_DAY, name="x").unchecked.count(
            "st_status") == 0

    def test_the_listing_exemption_is_named_when_undecidable(self):
        rule = market_rules("300001", REFORM_DAY, name="x")
        assert "listing_day_exemption" in rule.unchecked

    def test_a_settled_listing_question_is_not_reported_as_unchecked(self):
        rule = market_rules("300001", REFORM_DAY, name="x", listed_trading_days=40)
        assert "listing_day_exemption" not in rule.unchecked


class TestNormalisation:
    def test_exchange_prefixes_resolve_to_the_same_instrument(self):
        assert normalise_code("sh600000") == "600000"
        assert normalise_code("600000.SH") == "600000"
        assert normalise_code("600000") == "600000"
        assert normalise_code("sz300001") == "300001"

    def test_both_date_spellings_resolve(self):
        assert normalise_date("20200824") == "2020-08-24"
        assert normalise_date("2020-08-24") == "2020-08-24"

    def test_the_compact_spelling_gets_the_same_answer(self):
        compact = market_rules("300001", "20200824", name="x")
        dashed = market_rules("300001", "2020-08-24", name="x")
        assert compact == dashed

    def test_lot_size_is_one_hundred_shares(self):
        assert market_rules("600000", REFORM_DAY, name="x").lot_size == LOT_SIZE
