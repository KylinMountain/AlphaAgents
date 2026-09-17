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
        """A column the model is not told how to use is one it ignores."""
        text = D.load_prompt()
        assert "板块驱动" in text
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


class TestThePanelIsActuallyMixed:
    """The pool widening shipped broken and a counter caught it.

    The first version concatenated the turnover ranking after the change
    ranking and then took the first `limit` — so every slot was still filled
    from the change ranking and the widening did exactly nothing. The run
    reported `panel_offered_liquid: 0`, which is the only reason it was
    noticed.

    These tests pin the property that counter was trying to express: the
    panel must contain names that the change ranking alone would never have
    offered.
    """

    def _ctx(self):
        from collections import Counter
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf

        class _Corpus:
            instruments = {f"60000{i}": {"name": f"甲{i}"} for i in range(1, 9)}
            days = ["2026-01-05", "2026-01-06"]

            def bars(self, day):
                if day == "2026-01-06":
                    return {}
                # Alternating: high change / low turnover, and vice versa.
                return {
                    "600001": {"change_pct": 9.0, "close": 10.0, "turnover_rate": 1.0},
                    "600002": {"change_pct": 0.5, "close": 10.0, "turnover_rate": 40.0},
                    "600003": {"change_pct": 8.0, "close": 10.0, "turnover_rate": 2.0},
                    "600004": {"change_pct": 0.3, "close": 10.0, "turnover_rate": 35.0},
                    "600005": {"change_pct": 7.0, "close": 10.0, "turnover_rate": 3.0},
                    "600006": {"change_pct": 0.1, "close": 10.0, "turnover_rate": 30.0},
                    "600007": {"change_pct": 6.0, "close": 10.0, "turnover_rate": 4.0},
                    "600008": {"change_pct": 0.2, "close": 10.0, "turnover_rate": 25.0},
                }

            def previous(self, day):
                return "2026-01-05"

            def adv20(self, code, day):
                return 1_000_000

        class _Ctx:
            corpus = _Corpus()
            counters = Counter()
            _concepts = {}
            panel_size = 4

        return wf, _Ctx()

    def test_a_name_outside_the_top_by_change_is_offered(self, monkeypatch):
        wf, ctx = self._ctx()
        monkeypatch.setattr(wf, "_eligibility", lambda *a, **kw: None)
        panel = wf._build_panel(ctx, "2026-01-06", "2026-01-05", 4)
        codes = {p["code"] for p in panel}
        assert codes - {"600001", "600003", "600005", "600007"}, (
            "the panel is exactly the top-by-change cut, so the pool was not "
            "actually widened")

    def test_the_counter_reports_the_mix(self, monkeypatch):
        wf, ctx = self._ctx()
        monkeypatch.setattr(wf, "_eligibility", lambda *a, **kw: None)
        wf._build_panel(ctx, "2026-01-06", "2026-01-05", 4)
        assert ctx.counters["panel_offered_beyond_top_change"] > 0, (
            "the mix counter is zero: the two rankings were concatenated "
            "rather than interleaved, so the change ranking fills every slot")

    def test_turnover_is_carried_onto_every_row(self, monkeypatch):
        wf, ctx = self._ctx()
        monkeypatch.setattr(wf, "_eligibility", lambda *a, **kw: None)
        panel = wf._build_panel(ctx, "2026-01-06", "2026-01-05", 4)
        assert all("turnover_rate" in p for p in panel)
