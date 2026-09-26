"""What the agent is shown, and what it must never be shown.

The panel and the market block are the two additions that came out of the
first 20-day replay. Every order in it had been a bare
`code / close / change / adv20` row, ranked purely by the previous session's
change — five numbers, no sector, no breadth, and no way to tell a small-cap
theme stock from a large-cap blue chip.

Two things are being pinned here and they pull in opposite directions:

* the panel must carry **more** — turnover and concept membership, and a pool
  wider than "yesterday's top movers", because otherwise the ranking rather
  than the model chooses the strategy;
* it must still carry **nothing from after the decision day**, which is the
  property the whole runner exists to keep and the one a new data source is
  most likely to break.
"""

from __future__ import annotations

from alpha_agents.agents import t1_decider as D

PANEL = [
    {"code": "600001", "name": "甲", "close": 10.0, "change_pct": 3.0,
     "adv20": 1_000_000, "turnover_rate": 12.5, "concepts": ["半导体", "AI"]},
    {"code": "600002", "name": "乙", "close": 20.0, "change_pct": 1.0,
     "adv20": 500_000, "turnover_rate": 1.2, "concepts": []},
]


class TestThePanelCarriesWhatTheDecisionNeeds:
    def test_turnover_is_rendered(self):
        assert "12.5" in D.format_panel(PANEL)

    def test_concepts_are_rendered(self):
        assert "半导体、AI" in D.format_panel(PANEL)

    def test_a_name_with_no_concepts_shows_a_dash_not_a_blank(self):
        """A blank cell reads as a rendering bug; a dash reads as "not
        tagged", which is the fact."""
        row = [ln for ln in D.format_panel(PANEL).splitlines()
               if "600002" in ln][0]
        assert row.rstrip().endswith("| - |")

    def test_the_header_says_the_membership_is_current(self):
        """`concept_stocks` has no as-of column, so the tag is what the name
        is now, not what it was on the replayed session. The caveat belongs
        where the reader is, not only in a docstring."""
        assert "当前成分" in D.format_panel(PANEL)

    def test_an_empty_panel_says_so(self):
        assert "没有可交易的候选" in D.format_panel([])


class TestTheMarketBlock:
    def test_it_reports_breadth(self):
        text = D.format_market({"prev_day": "2026-08-19", "n": 5542,
                                "advancers_pct": 8.1,
                                "median_change_pct": -3.92,
                                "limit_up": 12, "limit_down": 40})
        assert "8.1%" in text and "-3.92%" in text and "12" in text

    def test_absence_is_stated_not_left_blank(self):
        """An empty section would read as "no breadth in the market"; the
        truth is "this run had none"."""
        assert "未提供" in D.format_market({})
        assert "未提供" in D.format_market(None)


class TestTheTemplateAsksForThem:
    def test_the_prompt_has_a_market_section(self):
        text = D.load_prompt()
        assert "{market}" in text

    def test_the_prompt_explains_why_concepts_matter(self):
        """A column the model is not told how to use is one it ignores.

        The claim it used to state as fact ("A 股是板块驱动的") is now a
        labelled prior: the model still reads how to use the column, and it
        also reads that nothing in this repository has tested the claim. See
        ``tests/test_policy_contamination.py``.
        """
        text = D.load_prompt()
        assert "脱离主线独自上涨的票" in text
        assert "〔先验" in text
        assert "换手" in text

    def test_it_renders_with_every_placeholder_consumed(self):
        msg = D.build_message(
            day="2026-08-20", prev_day="2026-08-19", panel=PANEL,
            news=[], book="", knowledge="", trader_note="", picks=2,
            template=D.load_prompt(),
            market={"prev_day": "2026-08-19", "n": 10, "advancers_pct": 50.0,
                    "median_change_pct": 0.1, "limit_up": 1, "limit_down": 1})
        assert "2026-08-19" in msg and "半导体" in msg
        assert "{market}" not in msg


class TestBreadthComesFromTheDatedCorpus:
    """`_market_state` reads `daily_kline`, not the snapshot tables.

    The snapshot tables start 2026-09-08, so a window opening 2026-08-18
    would have no breadth for its first fifteen sessions. The bars are dated
    rather than timestamped, which is what makes recomputing them safe.
    """

    def test_it_computes_advancers_and_limits(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf

        class _Corpus:
            def bars(self, day):
                return {
                    "600001": {"change_pct": 10.0, "close": 11.0},
                    "600002": {"change_pct": -10.0, "close": 9.0},
                    "600003": {"change_pct": 1.0, "close": 10.1},
                    "600004": {"change_pct": -1.0, "close": 9.9},
                }

        class _Ctx:
            corpus = _Corpus()

        state = wf._market_state(_Ctx(), "2026-08-19")
        assert state["n"] == 4
        assert state["advancers_pct"] == 50.0
        assert state["limit_up"] == 1 and state["limit_down"] == 1
        assert state["median_change_pct"] == 0.0

    def test_a_day_with_no_bars_returns_nothing_rather_than_zeroes(self):
        """An empty dict, not `advancers_pct: 0` — "no data" and "nothing
        rose" are different claims."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf

        class _Corpus:
            def bars(self, day):
                return {}

        class _Ctx:
            corpus = _Corpus()

        assert wf._market_state(_Ctx(), "2026-08-19") == {}


class TestConceptMembershipIsReadSafely:
    def test_a_missing_corpus_returns_a_mapping_not_an_error(self, monkeypatch):
        from alpha_agents.data import stock_meta
        monkeypatch.setattr(stock_meta, "_conn", None, raising=False)

        class _Boom:
            row_factory = None

            def execute(self, *a, **kw):
                import sqlite3
                raise sqlite3.DatabaseError("no such table")
        monkeypatch.setattr(stock_meta, "_get_conn", lambda: _Boom())
        assert stock_meta.concepts_by_code() == {}

    def test_the_cap_is_applied(self, monkeypatch):
        """A name tagged with 28 concepts is being listed, not described."""
        from alpha_agents.data import stock_meta

        class _Row(dict):
            def __getitem__(self, k):
                return dict.__getitem__(self, k)

        class _Conn:
            row_factory = None

            def execute(self, *a, **kw):
                return self

            def fetchall(self):
                return [_Row(code="600001", name=f"概念{i}") for i in range(28)]
        monkeypatch.setattr(stock_meta, "_get_conn", lambda: _Conn())
        got = stock_meta.concepts_by_code()
        assert len(got["600001"]) == stock_meta.MAX_CONCEPTS


class TestTheLimitStreakReachesThePanel:
    """The previous session's 涨停 list, which is A-share sentiment in a
    number: 首板 is a start, 二三板 is acceleration, a high board is
    relay risk. A 9% move means opposite things at 1 board and at 5.

    `limit_pool_snapshots` is **timestamped**, not dated, so the read is
    bounded by an instant. No capture exists before 10:56 on any session in
    this corpus (verified), so "the latest snapshot on the decision day"
    would always be one taken after the decision — the reader takes the
    previous session's evening captures instead.
    """

    def test_a_streak_is_rendered(self):
        row = dict(PANEL[0], consecutive_limits=3)
        assert "3板" in D.format_panel([row])

    def test_a_name_with_no_streak_shows_a_dash(self):
        row = dict(PANEL[0], consecutive_limits=None)
        line = [ln for ln in D.format_panel([row]).splitlines()
                if "600001" in ln][0]
        assert "| - |" in line

    def _seed(self, rows):
        """Plant limit-pool captures in the sandbox snapshot store."""
        from alpha_agents.data import snapshot_store as SS
        conn = SS._get_conn()
        SS._SCHEMA and conn.executescript(SS._SCHEMA)
        for captured_at, code, streak in rows:
            conn.execute(
                "INSERT INTO limit_pool_snapshots (captured_at, code, "
                "pool_type, name, consecutive_limits) VALUES (?,?,?,?,?)",
                (captured_at, code, "up", "甲", streak))
        conn.commit()

    def test_the_reader_takes_the_previous_sessions_close(self):
        """A 09:00 cutoff must find the capture from the evening before, not
        nothing. The first version bounded the read to the cutoff's own
        calendar day and returned 0 names while 73 sat in the table."""
        from alpha_agents.data import stock_meta
        self._seed([("2026-09-08 20:44:00", "600001", 3)])
        got = stock_meta.limit_pool_as_of("2026-09-09 09:00:00")
        assert got.get("600001", {}).get("consecutive_limits") == 3

    def test_a_capture_after_the_cutoff_is_not_read(self):
        """The lookahead guard: the decision is at 09:00 and this capture is
        from that afternoon."""
        from alpha_agents.data import stock_meta
        self._seed([("2026-09-09 15:30:00", "600002", 5)])
        assert stock_meta.limit_pool_as_of("2026-09-09 09:00:00") == {}

    def test_the_latest_capture_at_or_before_the_cutoff_wins(self):
        from alpha_agents.data import stock_meta
        self._seed([("2026-09-08 15:02:00", "600003", 1),
                    ("2026-09-08 20:44:00", "600003", 2)])
        got = stock_meta.limit_pool_as_of("2026-09-09 09:00:00")
        assert got["600003"]["consecutive_limits"] == 2

    def test_an_empty_store_returns_a_mapping_not_an_error(self):
        from alpha_agents.data import stock_meta
        assert stock_meta.limit_pool_as_of("2026-09-09 09:00:00") == {}

    def test_the_prompt_explains_board_counting(self):
        text = D.load_prompt()
        assert "连板" in text
        assert "首板" in text


class TestFundFlowReachesThePanel:
    """主力净额: price is the result, money is the reason.

    A name that rose on real institutional buying and one that rose while
    institutions sold into retail demand are the same green candle and
    different bets. The column comes from `stock_fund_flow_daily`, which is
    **dated** (`trade_date`), so the bound is the previous session and
    nothing later is reachable — unlike the timestamped snapshot tables,
    which need an explicit instant.
    """

    def test_net_amount_is_rendered_with_its_sign(self):
        row = dict(PANEL[0], net_amount=-35196.0)
        assert "-35,196" in D.format_panel([row])

    def test_inflow_is_signed_positive(self):
        row = dict(PANEL[0], net_amount=19441.0)
        assert "+19,441" in D.format_panel([row])

    def test_a_session_without_an_archive_shows_a_dash(self):
        """Blank means "not archived", which is not "no fund flow"."""
        row = dict(PANEL[0], net_amount=None)
        line = [ln for ln in D.format_panel([row]).splitlines()
                if "600001" in ln][0]
        assert line.count("| - |") >= 1

    def test_the_reader_is_bounded_by_the_session(self):
        """A day with no archive returns nothing rather than the newest
        rows, which is the lookahead guard."""
        from alpha_agents.data import stock_meta
        assert stock_meta.fund_flow_as_of("2020-01-01") == {}

    def test_the_reader_accepts_both_date_forms(self):
        """One query per panel rather than one per name."""
        from alpha_agents.data import stock_meta
        a = stock_meta.fund_flow_as_of("2026-09-16")
        b = stock_meta.fund_flow_as_of("20260916")
        assert a == b

    def test_it_can_be_limited_to_the_panel_codes(self):
        from alpha_agents.data import stock_meta
        got = stock_meta.fund_flow_as_of("2026-09-16", {"600584"})
        assert set(got) <= {"600584"}

    def test_the_prompt_explains_the_column(self):
        """The column is still explained; the causal claim is now labelled a
        prior rather than stated as a fact about the market."""
        text = D.load_prompt()
        assert "主力净额" in text
        assert "放量上涨且主力净流入" in text
        assert "〔先验" in text
