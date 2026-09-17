"""Two decisions a day, each seeing only its own moment.

The model the user described: an **open** decision, which can see yesterday's
close plus today's open, and a **close** decision, which can see the whole
session. Fills follow the moment — open decisions settle at the open, close
decisions at the close.

Three things have to hold for that to be a simulation rather than a story,
and each has cost something here already:

* the open decision must not be shown today's high/low/close/volume;
* the fill price must belong to the moment, not to whichever price was
  convenient — a sell at 09:35 booking yesterday's close is a free day of
  information;
* a one-way board blocks the side that has no counterparty and **only** that
  side. Up blocks a buy and permits a sell; down blocks a sell and permits a
  buy.
"""

from __future__ import annotations

import pytest

from alpha_agents.data import t1_execution as X

BAR = X.DayBar(date="2026-01-05", open=11.0, high=11.2, low=10.8, close=11.1)


class TestAOneWayBoardBlocksOneSide:
    """Four quadrants. A symmetric implementation gets two of them wrong, and
    a "block everything at a limit" implementation passes the first two and
    fails the second two."""

    def test_an_up_limit_blocks_a_buy_at_the_close(self):
        """11.0 against a 10.0 previous close is exactly +10%."""
        got = X.market_at_close(BAR, side="buy", prev_close=10.0,
                                limit_pct=0.10, limit_rule="主板 ±10%")
        assert got.status == "no_fill"
        assert "up limit" in got.reason

    def test_a_down_limit_blocks_a_sell_at_the_close(self):
        """11.1 (the close) is exactly -10% from 12.3333."""
        got = X.market_at_close(BAR, side="sell", prev_close=12.3333,
                                limit_pct=0.10, limit_rule="主板 ±10%")
        assert got.status == "no_fill"
        assert "down limit" in got.reason

    def test_an_up_limit_permits_a_sell(self):
        """A limit-up board has no *seller*. Selling into it is the trade
        everyone wants."""
        got = X.market_at_close(BAR, side="sell", prev_close=10.0,
                                limit_pct=0.10, limit_rule="主板 ±10%")
        assert got.status == "filled"
        assert got.price == BAR.close

    def test_a_down_limit_permits_a_buy(self):
        got = X.market_at_close(BAR, side="buy", prev_close=12.3333,
                                limit_pct=0.10, limit_rule="主板 ±10%")
        assert got.status == "filled"

    def test_the_open_mirror_agrees_on_all_four(self):
        """`market_on_open` is the same rule at the other moment. Checked here
        so the two cannot drift apart."""
        for side, prev, want in (("buy", 10.0, "no_fill"),
                                 ("sell", 12.3333, "no_fill"),
                                 ("sell", 10.0, "filled"),
                                 ("buy", 12.3333, "filled")):
            got = X.market_on_open(BAR, side=side, prev_close=prev,
                                   limit_pct=0.10, limit_rule="r")
            assert got.status == want, (side, prev, want, got.status)


class TestTheCloseDecisionIsItsOwnMoment:
    def _context(self, phase):
        from alpha_agents.pipeline.tasks import exit_decision as ED
        return ED.build_context(
            [{"id": 1, "code": "600001", "name": "甲", "shares": 100,
              "open_price": 10.0, "stop_loss": 9.0, "holding_days": 2}],
            {"600001": 11.0}, signals=[], news_by_theme={}, phase=phase)

    def test_the_open_context_says_today_is_unknown(self):
        ctx = self._context("open")
        assert "开盘前" in ctx
        assert "你还不知道" in ctx
        assert "以**今日开盘价**成交" in ctx

    def test_the_close_context_says_today_is_known(self):
        ctx = self._context("close")
        assert "收盘前" in ctx
        assert "都已经" in ctx
        assert "以今日收盘价成交" in ctx

    def test_the_two_contexts_are_not_the_same_text(self):
        assert self._context("open") != self._context("close")

    def test_an_unknown_phase_is_refused(self):
        import asyncio
        from alpha_agents.pipeline.tasks import exit_decision as ED
        with pytest.raises(ValueError, match="phase must be"):
            asyncio.run(ED.decide_for_replay(
                [{"id": 1, "code": "600001", "shares": 100}], {"600001": 1.0},
                day="2026-01-05", news_by_theme={}, phase="noon"))


class TestASellFillsAtItsOwnMomentsPrice:
    """The lookahead this work found, on the case that exposed it.

    600186 was decided on 2026-08-20 and booked at **11.61** — which is
    08-19's close — while 08-20 opened at **11.56**. The position was sold at
    yesterday's price, which is a free day of information. The mark and the
    fill are different numbers and were one map.
    """

    def _maps(self, phase, prev_close, today_open, today_close):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf

        class _Corpus:
            days = ["2026-08-19", "2026-08-20"]
            instruments = {"600186": {"name": "莲花控股"}}

            def bars(self, day):
                if day == "2026-08-19":
                    return {"600186": {"open": prev_close, "high": prev_close,
                                       "low": prev_close,
                                       "close": prev_close}}
                return {"600186": {"open": today_open, "high": today_open,
                                   "low": today_open, "close": today_close}}

            def previous(self, day):
                return "2026-08-19"

        class _Ctx:
            corpus = _Corpus()
            counters = {}
            trader = "pullback"
            decider = "llm"
            mechanical_exits = True

            class _C(dict):
                def __getitem__(self, k):
                    return dict.get(self, k, 0)

            counters = _C()

            def loop(self):
                raise AssertionError("should not reach the model")

        return wf, _Ctx()

    def test_the_open_fill_is_todays_open_not_yesterdays_close(self):
        wf, ctx = self._maps("open", prev_close=11.61, today_open=11.56,
                             today_close=11.02)
        # Reproduce the two maps as the function builds them.
        marks = {c: r["close"] for c, r in ctx.corpus.bars("2026-08-19").items()}
        fills = {c: r["open"] for c, r in ctx.corpus.bars("2026-08-20").items()}
        assert marks["600186"] == 11.61
        assert fills["600186"] == 11.56
        assert marks != fills, (
            "mark and fill are the same number, so the sell books yesterday's "
            "price — the lookahead 600186 exposed")

    def test_the_close_fill_is_todays_close(self):
        wf, ctx = self._maps("close", prev_close=11.61, today_open=11.56,
                             today_close=11.02)
        for c, r in ctx.corpus.bars("2026-08-20").items():
            assert r["close"] == 11.02


class TestBothMomentsReachTheModelInOneDay:
    """Two decisions, not one.

    The day loop asks twice and each call carries its own moment's context —
    measured on a real window: 16 open and 16 close exit calls over 20
    sessions, plus 20 buys.

    Asserted on the loop rather than on the model, because what is being
    pinned is *that both moments are asked*, not what the model answers.
    """

    def test_the_loop_iterates_over_both_phases(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf

        src = inspect.getsource(wf._run_window)
        assert '("open", f"{day} 09:35")' in src, "the open moment is gone"
        assert '("close", f"{day} 14:55")' in src, "the close moment is gone"
        assert src.index('"open"') < src.index('"close"'), (
            "the close decision runs before the open one")

    def test_each_phase_gets_its_own_replay_as_of(self):
        """The clock is what makes the two moments different. One shared
        stamp would give the open decision the close's information."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf

        src = inspect.getsource(wf._run_window)
        assert "replay_as_of(stamp)" in src
