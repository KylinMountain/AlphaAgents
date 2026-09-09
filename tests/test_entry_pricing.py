"""The entry price is a decision, and a decision can be refused.

109 of the system's 113 orders were priced at ``price * 0.97`` by a
scoring function with no agent in it. The constant was not conservative,
it was arbitrary: 中钨高新 moves 5.6% on an average day and 通源石油 6.1%,
so a 3% band on either is narrower than one ordinary session, and both
were cancelled as 价格已涨走.

These tests pin the property that replaces it: **no fallback price ever
reaches an order.** A timeout, a malformed block, an inverted zone, a stop
inside the entry band — every one of them places nothing. Half the value
of moving pricing to the agent is lost if a constant sneaks back in as an
error path.
"""

import json

import pytest

from alpha_agents.pipeline.tasks import entry_pricing as EP


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None
from alpha_agents.tools.price_levels import _atr_pct, _swings


def block(orders) -> str:
    return f"判断如下。\n\n<!-- ORDERS: {json.dumps(orders, ensure_ascii=False)} -->"


BUY = {"code": "600000", "action": "buy", "entry_low": 18.2,
       "entry_high": 18.9, "stop_loss": 17.4, "size_pct": 0.04,
       "reason": "回调到20日线18.3附近，atr 3.2%所以区间给3.8%宽",
       "confidence": "high"}


class TestParseOrders:
    def test_a_well_formed_order_survives(self):
        out = EP.parse_orders(block([BUY]))
        assert out["600000"]["entry_low"] == 18.2
        assert out["600000"]["stop_loss"] == 17.4
        assert out["600000"]["size_pct"] == 0.04

    def test_skip_is_a_real_answer(self):
        """The scorer finds what the money is buying; it does not judge the
        price. Declining every candidate is the system working."""
        out = EP.parse_orders(block([
            {"code": "600000", "action": "skip", "reason": "已在20日区间92%位置"}]))
        assert out == {}

    def test_no_block_places_nothing(self):
        assert EP.parse_orders("市场今天很弱，我不想买。") == {}

    def test_a_fenced_block_is_still_a_decision(self):
        """The model reliably produces the decision and unreliably produces
        the wrapper. A correct pass that fenced its JSON did not decline."""
        out = EP.parse_orders(
            "判断如下。\n\n```json\n" + json.dumps([BUY], ensure_ascii=False)
            + "\n```")
        assert out["600000"]["entry_low"] == 18.2

    def test_a_bare_array_is_still_a_decision(self):
        out = EP.parse_orders(
            "我的定价：\n" + json.dumps([BUY], ensure_ascii=False))
        assert out["600000"]["entry_low"] == 18.2

    def test_a_trailing_comma_is_repaired_not_refused(self):
        out = EP.parse_orders(
            "<!-- ORDERS: [" + json.dumps(BUY, ensure_ascii=False) + ",] -->")
        assert out["600000"]["entry_low"] == 18.2

    def test_truly_malformed_json_places_nothing(self):
        assert EP.parse_orders("<!-- ORDERS: [{{{{ -->") == {}

    @pytest.mark.parametrize("missing", ["entry_low", "entry_high", "stop_loss"])
    def test_a_missing_level_is_dropped_not_defaulted(self, missing):
        """The whole point. A repaired order is the constant coming back."""
        bad = {**BUY}
        bad.pop(missing)
        assert EP.parse_orders(block([bad])) == {}

    def test_an_inverted_zone_is_dropped(self):
        assert EP.parse_orders(block([
            {**BUY, "entry_low": 19.5, "entry_high": 18.0}])) == {}

    def test_a_stop_inside_the_entry_zone_is_dropped(self):
        """A stop above the entry price is triggered the moment it fills."""
        assert EP.parse_orders(block([{**BUY, "stop_loss": 18.5}])) == {}

    def test_an_unexplained_order_is_dropped(self):
        """复盘 grades the reason. An order with none cannot be graded."""
        assert EP.parse_orders(block([{**BUY, "reason": ""}])) == {}

    def test_a_zero_price_is_not_a_price(self):
        assert EP.parse_orders(block([{**BUY, "stop_loss": 0}])) == {}

    def test_size_may_be_omitted(self):
        """Sizing has a per-trader default; the entry price has none."""
        thin = {k: v for k, v in BUY.items() if k != "size_pct"}
        assert EP.parse_orders(block([thin]))["600000"]["size_pct"] is None


class TestPriceRefusesRatherThanGuesses:
    @pytest.mark.asyncio
    async def test_no_candidates_no_call(self):
        assert await EP.price([], object()) == {}

    @pytest.mark.asyncio
    async def test_a_failing_model_places_nothing(self, monkeypatch):
        class Boom:
            @staticmethod
            async def run(*a, **k):
                raise RuntimeError("模型挂了")

        monkeypatch.setattr("agents.Runner", Boom)
        trader = type("T", (), {"id": "x", "max_position_pct": 0.1,
                                "extra_prompt": ""})()
        out = await EP.price([{"code": "600000", "price": 10.0}], trader)
        assert out == {}


class TestPriceLevelsAreFactsNotAdvice:
    def test_atr_measures_the_daily_range(self):
        bars = [{"close": 10 + i * 0.1, "high": 10.5 + i * 0.1,
                 "low": 9.5 + i * 0.1} for i in range(20)]
        atr = _atr_pct(bars)
        assert atr is not None and 5 < atr < 12

    def test_atr_is_none_without_enough_history(self):
        """Better no number than a number off three bars."""
        assert _atr_pct([{"close": 10, "high": 10, "low": 10}]) is None

    def test_swing_lows_are_where_the_market_turned(self):
        closes = [12, 11, 10, 11, 12, 13, 12, 11, 12, 13]
        assert 10 in _swings(closes, want_low=True)

    def test_the_tool_says_it_gives_no_advice(self):
        """A suggested entry price would just replace one constant with
        another and teach the agent nothing."""
        from alpha_agents.tools import price_levels
        src = (price_levels.__file__)
        text = open(src, encoding="utf-8").read()
        assert "不含任何建议" in text


class TestEntrySideIsMeasuredNotEnforced:
    """Style is a prompt now, and a prompt can be ignored — so it is counted.

    Asked to price the same stock, the trader told to buy strength wrote
    "等回调至20日低点33.42附近" and placed below the market. Nothing in the
    code forbids that; ``entry_style`` used to, and removing it is the
    point. What replaces the enforcement is measurement.
    """

    def _order(self, store, code, lo, hi, ref, trader="t1"):
        from datetime import date as _date

        from alpha_agents.data.memory_store import _get_conn, save_prediction
        today = _date.today().isoformat()
        save_prediction(date=today, report_type="morning", code=code,
                        name="X", direction="bullish", confidence="high",
                        theme_line="t", entry_price=ref, reason="r",
                        trader_id=trader)
        conn = _get_conn()
        conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, entry_low, entry_high, status, "
            " trader_id) VALUES (?,?,?,?,?,?,'pending',?)",
            (code, "X", "t", today, lo, hi, trader))
        conn.commit()

    def test_it_counts_which_side_of_the_market(self, store):
        from alpha_agents.data.portfolio_risk import entry_side
        self._order(store, "600001", 9.0, 9.7, 10.0)    # below
        self._order(store, "600002", 10.3, 11.0, 10.0)  # above
        self._order(store, "600003", 9.8, 10.2, 10.0)   # straddle
        s = entry_side(trader_id="t1")
        assert (s["below"], s["above"], s["straddle"]) == (1, 1, 1)

    def test_one_traders_orders_do_not_count_for_another(self, store):
        from alpha_agents.data.portfolio_risk import entry_side
        self._order(store, "600001", 9.0, 9.7, 10.0, trader="t1")
        self._order(store, "600002", 10.3, 11.0, 10.0, trader="t2")
        assert entry_side(trader_id="t1")["below"] == 1
        assert entry_side(trader_id="t2")["above"] == 1

    def test_a_thin_sample_says_nothing(self, store):
        """Three orders cannot show a style, and claiming they do would
        teach the agent to correct a bias it does not have."""
        from alpha_agents.data.portfolio_risk import inject_entry_side
        self._order(store, "600001", 9.0, 9.7, 10.0)
        assert inject_entry_side(trader_id="t1") == ""
