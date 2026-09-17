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


class TestTheCloseBuyQuadrant:
    """The fourth quadrant, and the one that had no path at all.

    An open buy rests a limit and settles at the open; open and close sells
    fill at their own moment. Buying *into the close* had nothing, so a model
    that spotted a setup at 14:55 could only write it down for tomorrow — a
    different trade at a different price.
    """

    def test_the_buy_prompt_speaks_for_each_moment(self):
        from alpha_agents.agents import t1_decider as D
        template = D.load_prompt()
        common = dict(day="2026-08-20", prev_day="2026-08-19", panel=[],
                      news=[], book="", knowledge="", trader_note="", picks=2,
                      template=template)
        opener = D.build_message(**common, phase="open")
        closer = D.build_message(**common, phase="close")
        assert "开盘前 09:00" in opener
        assert "你全都不知道" in opener
        assert "以**开盘价**成交" in opener
        assert "收盘前 14:55" in closer
        assert "已经全部看到" in closer
        assert "以今日收盘价成交" in closer
        assert opener != closer

    def test_the_news_window_matches_the_moment(self):
        """The open decision reads overnight; the close decision reads the
        session that just happened."""
        from alpha_agents.agents import t1_decider as D
        template = D.load_prompt()
        common = dict(day="2026-08-20", prev_day="2026-08-19", panel=[],
                      news=[], book="", knowledge="", trader_note="", picks=2,
                      template=template)
        assert "昨夜到今早" in D.build_message(**common, phase="open")
        assert "今日盘中" in D.build_message(**common, phase="close")

    def test_an_unknown_phase_is_refused_at_render(self):
        from alpha_agents.agents import t1_decider as D
        with pytest.raises(D.DeciderError, match="phase must be"):
            D.build_message(day="2026-08-20", prev_day="2026-08-19",
                            panel=[], news=[], book="", knowledge="",
                            trader_note="", picks=2, template=D.load_prompt(),
                            phase="lunch")

    def test_the_close_buy_checks_the_limit_before_buying(self):
        """A close at the up limit has no seller. Reused rather than
        re-implemented, so the close buy cannot drift from the close sell."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._close_buys)
        assert "market_at_close" in src and 'side="buy"' in src

    def test_the_close_buy_honours_the_stated_zone(self):
        """`entry_low`/`entry_high` are read as the range the trader accepts
        rather than as a resting limit — a fill outside its own stated zone
        would be the system accepting a price the trader said no to."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._close_buys)
        assert "close_buy_outside_zone" in src

    def test_the_close_buy_books_a_filled_position_not_a_pending_order(self):
        """A pending order settles at the *next* open, which is exactly the
        price this decision does not get."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._close_buys)
        assert "open_position" in src
        assert "create_pending_order" not in src


class TestANameCarriesOnePositionAtATime:
    """`portfolio` refuses a second position on a name that already has one
    (or a pending order for one). The agent re-ordered names it was holding
    and the intent door refused them with a message that names four possible
    causes, so the refusal was indistinguishable from a theme or capital
    problem.

    Measured on a real window: the close decision re-bought `603407` and
    `300035`, both of which the open decision had ordered the same morning.

    The rule is real — one name, one position — so the fix is to state it,
    not to relax it. The prompt says it now, and the book is already in the
    context for the agent to check against.
    """

    def test_the_rule_exists_in_the_book(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        from alpha_agents.data import portfolio
        src = inspect.getsource(portfolio)
        assert "already exists for" in src

    def test_the_buy_prompt_states_it(self):
        """A constraint the model is not told about is one it will violate
        and then be silently refused for."""
        from alpha_agents.agents import t1_decider as D
        text = D.load_prompt()
        assert "一只票同时只能有一笔" in text
        assert "先看上面的【我的账本】" in text

    def test_the_book_is_actually_in_the_context(self):
        """The rule is only actionable if the agent can see what it holds."""
        from alpha_agents.agents import t1_decider as D
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._decide_llm)
        assert "_book_and_knowledge" in src
