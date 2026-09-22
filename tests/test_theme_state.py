"""A theme condition must read the theme's state, and the same one everywhere.

``theme_flow_negative`` and ``theme_rank_worse_than`` ask whether a theme is
still leading and still attracting money. Both were handed one session's
number, and one session of concept flow carries no information about the
next: measured over 2026-01-05..03-31, the day-over-day Spearman of the whole
ranking is −0.004. The consequences were not subtle — the flow condition rode
on 34 of 34 theses a replay wrote and fires within three sessions 99.2% of the
time on a single day's net. Every position carried a timer disguised as a
thesis.

The two producers also disagreed. The replay ranked one session of synthesized
concept flow; production ranked a live concept API call. A backtest that
measures a rule production does not run measures nothing.
"""

import sqlite3

import pytest

from alpha_agents.data import theme_state as TS


class TestTheWindowNeverReachesForward:
    """PIT safety, at the one place that decides which sessions count."""

    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE stock_fund_flow_daily (trade_date TEXT, code TEXT)")
        c.executemany("INSERT INTO stock_fund_flow_daily VALUES (?, '000001')",
                      [(d,) for d in ("20260105", "20260106", "20260107",
                                      "20260108", "20260109", "20260112",
                                      "20260113")])
        return c

    def test_the_window_ends_at_the_asked_session(self, conn):
        got = TS._sessions_ending(conn, "20260108", 5)
        assert max(got) == "20260108"

    def test_a_later_session_is_never_included(self, conn):
        got = TS._sessions_ending(conn, "20260107", 5)
        assert not [d for d in got if d > "20260107"]

    def test_the_window_is_as_long_as_asked(self, conn):
        assert len(TS._sessions_ending(conn, "20260113", 5)) == 5

    def test_a_short_history_gives_what_there_is(self, conn):
        """Early in a replay there are fewer sessions; that is not an error."""
        assert TS._sessions_ending(conn, "20260106", 5) == ["20260106", "20260105"]

    def test_holidays_need_no_calendar(self, conn):
        """20260110 and 11 are a weekend and simply absent from the data."""
        assert TS._sessions_ending(conn, "20260112", 3) == [
            "20260112", "20260109", "20260108"]


class TestAFailedReadMeasuresNothing:
    def test_missing_databases_yield_empty_not_partial(self, monkeypatch, tmp_path):
        """MarketView reads an absent key as "could not check". A half-filled
        ranking would instead read as "your theme is unranked" and fire."""
        monkeypatch.setattr("alpha_agents.config.DATA_DIR", tmp_path)
        monkeypatch.setattr("alpha_agents.config.DB_PATH", tmp_path / "none.db")
        ranks, flows = TS.concept_state("2026-01-20")
        assert ranks == {} and flows == {}

    def test_the_failure_is_logged_with_the_date(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr("alpha_agents.config.DATA_DIR", tmp_path)
        monkeypatch.setattr("alpha_agents.config.DB_PATH", tmp_path / "none.db")
        with caplog.at_level("WARNING"):
            TS.concept_state("2026-01-20")
        assert "2026-01-20" in caplog.text


class TestBothProducersAskTheSameQuestion:
    def test_the_replay_reads_the_window(self):
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        src = inspect.getsource(wf._check_theses)
        assert "concept_state" in src
        assert "get_concept_fund_flow" not in src, "the single-session read is back"

    def test_production_reads_the_window(self):
        import inspect
        from alpha_agents.pipeline.tasks import intraday_monitor as im
        src = inspect.getsource(im._market_view)
        assert "concept_state" in src
        assert "get_concept_ranking_fn" not in src, "the single-session read is back"

    def test_the_window_is_one_constant_not_two_literals(self):
        assert TS.WINDOW_SESSIONS == 5


class TestTheAgentIsGivenTheBaseRate:
    def _line(self, kind):
        from alpha_agents.data.thesis import prompt_vocabulary
        return next(ln for ln in prompt_vocabulary().splitlines() if kind in ln)

    def test_the_flow_condition_says_it_is_cumulative(self):
        line = self._line("theme_flow_negative")
        assert "5 日累计" in line and "实测" in line

    def test_the_rank_condition_admits_it_measures_rotation(self):
        """The number that matters: 96.9% within five sessions."""
        line = self._line("theme_rank_worse_than")
        assert "97%" in line
        assert "1–2 日" in line, "the horizon it is actually usable on"
