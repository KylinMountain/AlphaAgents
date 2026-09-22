"""The number the agent reads and the number it is judged against must match.

The direction table renders a sector's fund flow in a row whose other cells
are all labelled 5日. It was one session. The agent read it the way the row
reads and wrote its invalidation notes accordingly — "5日净额合计
1995963.71" for a value covering a single day — while ``theme_state`` handed
the evaluator a genuine five-session cumulative.

Measured on a 20-day autonomous replay, the two disagreed in sign, not just
in magnitude:

    金属铜 000737  note said +69.66亿   evaluator saw -110.2亿
    金属锌 601168  note said +42.3亿    evaluator saw  -46.5亿
    CRO概念 603259 note said -15.0亿    evaluator saw  -87.3亿

and **8 of 25 theses were created with a theme_flow_negative condition that
was already true on the day the position opened** — a thesis that invalidates
itself before the fill.

Ranking is untouched by this: ``rank_sector_snapshots`` orders on relative
return, advancers and breadth improvement, never on the flow sum. So the
column is informational, and aligning its window changes what the agent is
told without changing what it is offered.
"""

import sqlite3

import pytest

from alpha_agents.data import stock_meta


@pytest.fixture
def archive(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE stock_fund_flow_daily ("
                 "code TEXT, name TEXT, trade_date TEXT, net_amount REAL, "
                 "net_amount_rate REAL, buy_lg_amount REAL, buy_elg_amount REAL)")
    rows = [
        ("000001", "平安", "20260105", 100.0, 1.1, 10.0, 5.0),
        ("000001", "平安", "20260106", -30.0, 2.2, 8.0, 4.0),
        ("000001", "平安", "20260107", 50.0, 3.3, 6.0, 3.0),
        ("000002", "万科", "20260107", -20.0, 9.9, 1.0, 1.0),
    ]
    conn.executemany("INSERT INTO stock_fund_flow_daily VALUES (?,?,?,?,?,?,?)",
                     rows)

    # Patch the accessor, not the module. ``stock_meta`` does
    # ``from alpha_agents.data import snapshot_store``, which reads the
    # attribute already bound on the package, so replacing the sys.modules
    # entry has no effect once anything else has imported it — which passes
    # alone and fails in a full run.
    monkeypatch.setattr("alpha_agents.data.snapshot_store._get_conn",
                        lambda: conn)
    return conn


class TestTheWindow:
    def test_one_session_is_still_the_default(self, archive):
        got = stock_meta.fund_flow_as_of("2026-01-07", codes={"000001"})
        assert got["000001"]["net_amount"] == 50.0

    def test_a_window_accumulates(self, archive):
        got = stock_meta.fund_flow_as_of("2026-01-07", codes={"000001"},
                                         sessions=3)
        assert got["000001"]["net_amount"] == 120.0   # 100 - 30 + 50
        assert got["000001"]["sessions"] == 3

    def test_the_rate_is_the_last_session_not_a_sum(self, archive):
        """A rate is one day's intensity; adding three of them means nothing."""
        got = stock_meta.fund_flow_as_of("2026-01-07", codes={"000001"},
                                         sessions=3)
        assert got["000001"]["net_amount_rate"] == 3.3

    def test_nothing_after_the_bound_is_reached(self, archive):
        got = stock_meta.fund_flow_as_of("2026-01-06", codes={"000001"},
                                         sessions=5)
        assert got["000001"]["net_amount"] == 70.0    # 100 - 30, not +50
        assert got["000001"]["sessions"] == 2

    def test_a_code_present_on_only_one_session_is_not_dropped(self, archive):
        got = stock_meta.fund_flow_as_of("2026-01-07", sessions=3)
        assert got["000002"]["net_amount"] == -20.0
        assert got["000002"]["sessions"] == 1

    def test_an_unarchived_bound_measures_nothing(self, archive):
        assert stock_meta.fund_flow_as_of("2025-12-01", sessions=3) == {}


class TestBothSidesUseTheSameWindow:
    def test_the_table_reads_the_theme_state_window(self):
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        src = inspect.getsource(wf._fund_flow_map)
        assert "WINDOW_SESSIONS" in src, (
            "the agent's column and the evaluator must share one window")
        assert "sessions=WINDOW_SESSIONS" in src

    def test_the_column_names_its_window(self):
        """The old header let 净额合计 sit beside four 5日 cells and mean
        something else. The agent's own notes show it read the row, not the
        docstring."""
        from alpha_agents.agents import sector_selector
        import inspect
        src = inspect.getsource(sector_selector.format_sector_cards)
        header = next(ln for ln in src.splitlines() if "排名|方向" in ln)
        assert "近5日净额合计" in header

    def test_ranking_does_not_read_the_flow_sum(self):
        """Why changing the window is safe: it is shown, never ranked on."""
        import inspect
        from alpha_agents.data import sector_selection
        src = inspect.getsource(sector_selection.rank_sector_snapshots)
        assert "net_amount" not in src
