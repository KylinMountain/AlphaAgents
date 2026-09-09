"""Two traders are only an experiment if their books cannot touch.

The whole reason for this feature is comparison: run 回调 and 突破 against
the same market and see which book compounds. Every leak between them
destroys the thing being measured — shared capital measures ordering,
shared positions measure whoever ran first, a pooled calibration curve
describes an average nobody traded.

So these tests are mostly about isolation, plus the two places where
sharing is deliberate: the market is common, and a book that predates
traders/ must keep being managed rather than orphaned.
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import portfolio as P
from alpha_agents.data import thesis as T
from alpha_agents.data import trader as TR
from alpha_agents.evolution import calibration as C


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


@pytest.fixture()
def traders_dir(tmp_path, monkeypatch):
    d = tmp_path / "traders"
    d.mkdir()
    monkeypatch.setattr(TR, "TRADERS_DIR", d, raising=False)
    return d


def write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


@pytest.fixture()
def theme():
    """A tradable world: one tracked theme, and no network.

    An order is refused unless its theme is one the system tracks, and
    a fill reads the sentiment cycle — which goes to the market. Pinned
    here so these tests measure the trader split and nothing else.
    """
    row = {"name": "t", "status": "active", "strength": 6,
           "daily_score": 1, "core_stocks": "[]"}
    with patch("alpha_agents.data.memory_store.get_active_themes",
               return_value=[row]), \
         patch("alpha_agents.data.memory_store.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.portfolio.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
               return_value={"phase": "修复",
                             "strategy": {"max_exposure_pct": 100}}):
        yield row


PULLBACK = """
id: slow
name: 回调派
capital: 500000
"""

BREAKOUT = """
id: fast
name: 突破派
capital: 300000
default_size_pct: 0.05
max_position_pct: 0.12
extra_prompt: |
  你追时机，不追价格。
"""


class TestConfigLoading:
    def test_empty_directory_is_the_single_trader_case(self, traders_dir):
        """Not an error. The feature costs nothing until a file appears."""
        traders = TR.load_traders()
        assert [t.id for t in traders] == [TR.DEFAULT_TRADER]

    def test_example_files_are_inert(self, traders_dir):
        (traders_dir / "sample.yaml.example").write_text("id: nope\n")
        assert [t.id for t in TR.load_traders()] == [TR.DEFAULT_TRADER]

    def test_a_file_is_a_trader(self, traders_dir):
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "fast", BREAKOUT)
        ids = {t.id for t in TR.load_traders(scanning=True)}
        assert ids == {"slow", "fast"}

    def test_disabled_trader_is_left_out(self, traders_dir):
        write(traders_dir, "slow", PULLBACK + "enabled: false\n")
        assert [t.id for t in TR.load_traders()] == [TR.DEFAULT_TRADER]

    def test_a_bad_field_type_is_refused_not_defaulted(self, traders_dir):
        """Silently defaulting would trade real capital under a name that
        promises something else."""
        write(traders_dir, "weird", "id: weird\ncapital: 很多钱\n")
        assert [t.id for t in TR.load_traders()] == [TR.DEFAULT_TRADER]

    def test_a_file_cannot_claim_the_default_id(self, traders_dir):
        write(traders_dir, "x", "id: default\n")
        assert [t.id for t in TR.load_traders()] == [TR.DEFAULT_TRADER]

    def test_malformed_yaml_drops_one_file_not_all(self, traders_dir):
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "broken", "id: [unclosed\n")
        assert [t.id for t in TR.load_traders(scanning=True)] == ["slow"]

    def test_unknown_trader_falls_back_rather_than_raising(self, traders_dir):
        """A position whose config was deleted still has to be managed."""
        assert TR.get_trader("ghost").id == TR.DEFAULT_TRADER


class TestStrategyLivesInThePrompt:
    """A trader carries no prices. It carries instructions and a budget.

    ``entry_style`` used to live here as a three-value enum, on the theory
    that a prompt cannot say where an order sits relative to the market. It
    can — and the agent has ``get_price_levels`` to compute against, so a
    config that fixed the answer only capped what a trader could become.
    """

    def test_own_instructions_reach_the_prompt(self, traders_dir):
        write(traders_dir, "fast", BREAKOUT)
        prompt = TR.get_trader("fast").prompt()
        assert "你追时机，不追价格。" in prompt
        assert "## 你是谁" in prompt

    def test_a_trader_carries_no_entry_price(self, traders_dir):
        """The regression guard: no field here may decide where to buy."""
        write(traders_dir, "slow", PULLBACK)
        t = TR.get_trader("slow")
        assert not hasattr(t, "entry_zone")
        assert not hasattr(t, "entry_style")

    def test_budget_and_backstops_do_stay_in_config(self, traders_dir):
        """What the agent cannot compute for itself: its own money."""
        write(traders_dir, "fast", BREAKOUT)
        t = TR.get_trader("fast")
        assert t.capital == 300_000
        assert t.default_size_pct == 0.05
        assert t.max_position_pct == 0.12


class TestCapitalIsolation:
    def test_each_trader_spends_its_own_money(self, store, traders_dir):
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "fast", BREAKOUT)
        assert P.trader_capital("slow") == 500_000
        assert P.trader_capital("fast") == 300_000

    def test_one_trader_filling_does_not_reduce_the_other(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "fast", BREAKOUT)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, trader_id="slow")
        assert oid
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id="slow")
        assert P.get_available_capital("fast") == 300_000
        assert P.get_available_capital("slow") < 500_000

    def test_a_traders_pass_only_sees_its_own_orders(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "fast", BREAKOUT)
        P.create_pending_order(code="600000", name="A", theme="t",
                               order_date="2026-01-05", entry_low=9.0,
                               entry_high=11.0, trader_id="slow")
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id="fast")
        assert P.get_open_positions("fast") == []
        assert P.get_pending_orders("slow")

    def test_two_traders_may_hold_the_same_stock(self, store, traders_dir,
                                                theme):
        """That is the comparison working, not a duplicate."""
        write(traders_dir, "slow", PULLBACK)
        write(traders_dir, "fast", BREAKOUT)
        assert P.create_pending_order(code="600000", name="A", theme="t",
                                      order_date="2026-01-05",
                                      trader_id="slow")
        assert P.create_pending_order(code="600000", name="A", theme="t",
                                      order_date="2026-01-05",
                                      trader_id="fast")

    def test_one_trader_may_not_hold_it_twice(self, store, traders_dir,
                                             theme):
        write(traders_dir, "slow", PULLBACK)
        assert P.create_pending_order(code="600000", name="A", theme="t",
                                      order_date="2026-01-05",
                                      trader_id="slow")
        assert P.create_pending_order(code="600000", name="A", theme="t",
                                      order_date="2026-01-05",
                                      trader_id="slow") is None


class TestLearningIsPerTrader:
    def _closed(self, trader_id, prob, status, code="000001"):
        tid = T.create(T.Thesis(code=code, name="X", theme="t", claim="c",
                                prob=prob, conviction=0.5,
                                trader_id=trader_id,
                                conditions=[T.Condition("price_below", 10)]))
        T.close(tid, status)

    def test_one_traders_curve_excludes_the_others(self, store):
        for _ in range(4):
            self._closed("slow", 0.75, T.VALIDATED)
        for _ in range(4):
            self._closed("fast", 0.75, T.INVALIDATED)

        slow = next(r for r in C.calibration(trader_id="slow")
                    if r["bucket"] == ">70%")
        fast = next(r for r in C.calibration(trader_id="fast")
                    if r["bucket"] == ">70%")
        assert slow["hit_rate"] == 1.0
        assert fast["hit_rate"] == 0.0

    def test_pooling_them_describes_a_trader_nobody_ran(self, store):
        """Kept as a test because it is the failure mode, not a feature:
        without a trader_id the curve averages two strategies."""
        for _ in range(4):
            self._closed("slow", 0.75, T.VALIDATED)
        for _ in range(4):
            self._closed("fast", 0.75, T.INVALIDATED)
        pooled = next(r for r in C.calibration() if r["bucket"] == ">70%")
        assert pooled["hit_rate"] == pytest.approx(0.5)

    def test_active_theses_are_scoped_too(self, store):
        T.create(T.Thesis(code="000001", trader_id="slow",
                          conditions=[T.Condition("price_below", 10)]))
        T.create(T.Thesis(code="000002", trader_id="fast",
                          conditions=[T.Condition("price_below", 10)]))
        assert [t.code for t in T.get_active(trader_id="slow")] == ["000001"]
        assert len(T.get_active()) == 2

    def test_the_same_stock_gets_a_thesis_per_trader(self, store):
        from alpha_agents.data.thesis import from_recommendation
        rec = {"name": "A", "theme": "t", "claim": "涨", "prob": 0.6,
               "invalidations": [{"kind": "price_below", "value": 9}]}
        first = from_recommendation(rec, "600000", "morning",
                                    trader_id="slow")
        second = from_recommendation(rec, "600000", "morning",
                                     trader_id="fast")
        assert first and second and first != second
        # ...but one trader re-recommending it does not write a second.
        again = from_recommendation(rec, "600000", "morning",
                                    trader_id="slow")
        assert again == first


class TestLegacyBook:
    """The first config file must not orphan the positions already open."""

    def test_default_is_kept_while_it_still_holds_something(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", PULLBACK)
        P.create_pending_order(code="600000", name="A", theme="t",
                               order_date="2026-01-05")
        ids = [t.id for t in TR.load_traders()]
        assert TR.DEFAULT_TRADER in ids

    def test_but_it_no_longer_opens_new_positions(self, store, traders_dir,
                                                 theme):
        write(traders_dir, "slow", PULLBACK)
        P.create_pending_order(code="600000", name="A", theme="t",
                               order_date="2026-01-05")
        scanning = [t.id for t in TR.load_traders(scanning=True)]
        assert TR.DEFAULT_TRADER not in scanning

    def test_and_it_disappears_once_the_book_is_empty(self, store,
                                                     traders_dir):
        write(traders_dir, "slow", PULLBACK)
        assert [t.id for t in TR.load_traders()] == ["slow"]
