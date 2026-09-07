"""The learning loop: dedup, benchmark-adjusted grading, close→prediction."""

import sqlite3
from unittest.mock import patch

import pytest

from alpha_agents.pipeline.tasks.review import _market_return_pct


class TestMarketReturnBenchmark:
    """Hit rate must measure skill, not the day's beta."""

    def test_median_of_quoted_changes(self):
        rt = {str(i): {"change_pct": c} for i, c in enumerate([-2.0, 0.0, 1.0, 3.0, 8.0])}
        assert _market_return_pct(rt) == 1.0

    def test_even_count_averages_the_middle_pair(self):
        rt = {str(i): {"change_pct": c} for i, c in enumerate([0.0, 1.0, 3.0, 4.0, 5.0, 6.0])}
        assert _market_return_pct(rt) == 3.5

    def test_uses_median_not_mean(self):
        # One limit-up would drag a mean well above the typical stock.
        rt = {str(i): {"change_pct": c} for i, c in enumerate([0.0, 0.0, 0.0, 0.0, 10.0])}
        assert _market_return_pct(rt) == 0.0

    def test_too_few_quotes_degrades_to_zero(self):
        rt = {"1": {"change_pct": 5.0}, "2": {"change_pct": 3.0}}
        assert _market_return_pct(rt) == 0.0

    def test_ignores_missing_and_non_numeric(self):
        rt = {
            "1": {"change_pct": 1.0}, "2": {"change_pct": 2.0},
            "3": {"change_pct": 3.0}, "4": {"change_pct": 4.0},
            "5": {"change_pct": 5.0}, "6": {}, "7": {"change_pct": None},
        }
        assert _market_return_pct(rt) == 3.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A memory_store backed by a scratch DB."""
    from alpha_agents.data import memory_store as ms

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(ms._SCHEMA)
    try:
        conn.execute("ALTER TABLE predictions ADD COLUMN features_json TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    monkeypatch.setattr(ms, "_get_conn", lambda: conn)
    yield ms, conn
    conn.close()


class TestPredictionDedup:
    """Intraday re-saves its top-5 every cycle; one row per stock per day."""

    def test_repeat_saves_collapse_to_one_row(self, store):
        ms, conn = store
        ids = [
            ms.save_prediction(
                date="2026-09-07", report_type="intraday", code="300308",
                name="中际旭创", direction="看多", confidence="high",
                theme_line="AI算力", entry_price=150.0, reason=f"第{i}轮",
            )
            for i in range(1, 6)
        ]
        assert len(set(ids)) == 1
        n = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE date='2026-09-07' AND code='300308'"
        ).fetchone()[0]
        assert n == 1

    def test_later_cycle_refreshes_the_row(self, store):
        ms, conn = store
        kw = dict(date="2026-09-07", report_type="intraday", code="300308",
                  name="中际旭创", direction="看多", theme_line="AI算力",
                  entry_price=150.0)
        ms.save_prediction(confidence="medium", reason="首轮", **kw)
        ms.save_prediction(confidence="high", reason="尾盘加强", **kw)
        row = conn.execute(
            "SELECT confidence, reason FROM predictions WHERE code='300308'"
        ).fetchone()
        assert row["confidence"] == "high"
        assert row["reason"] == "尾盘加强"

    def test_entry_price_is_not_lost_on_refresh(self, store):
        ms, conn = store
        kw = dict(date="2026-09-07", report_type="intraday", code="300308",
                  name="中际旭创", direction="看多", confidence="high",
                  theme_line="AI算力", reason="r")
        pid = ms.save_prediction(entry_price=150.0, **kw)
        ms.save_prediction(entry_price=None, **kw)   # a later cycle with no quote
        row = conn.execute(
            "SELECT id, entry_price FROM predictions WHERE code='300308'"
        ).fetchone()
        assert row["id"] == pid
        assert row["entry_price"] == 150.0

    def test_different_report_types_stay_separate(self, store):
        ms, conn = store
        kw = dict(date="2026-09-07", code="300308", name="中际旭创",
                  direction="看多", confidence="high", theme_line="AI算力",
                  entry_price=150.0, reason="r")
        ms.save_prediction(report_type="morning", **kw)
        ms.save_prediction(report_type="intraday", **kw)
        n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        assert n == 2

    def test_different_dates_stay_separate(self, store):
        ms, conn = store
        kw = dict(report_type="intraday", code="300308", name="中际旭创",
                  direction="看多", confidence="high", theme_line="AI算力",
                  entry_price=150.0, reason="r")
        ms.save_prediction(date="2026-09-07", **kw)
        ms.save_prediction(date="2026-09-08", **kw)
        n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        assert n == 2


class TestCloseFeedsLearning:
    """A realised round trip overwrites the next-day proxy."""

    def _seed(self, ms, conn, *, return_pct):
        pid = ms.save_prediction(
            date="2026-09-01", report_type="intraday", code="300308",
            name="中际旭创", direction="看多", confidence="high",
            theme_line="AI算力", entry_price=150.0, reason="r",
        )
        conn.execute(
            "INSERT INTO virtual_portfolio (id, code, name, theme, order_date, "
            "open_date, open_price, shares, status) "
            "VALUES (1, '300308', '中际旭创', 'AI算力', '2026-09-01', "
            "'2026-09-01', 150.0, 100, 'closed')"
        )
        conn.commit()
        return pid

    def test_loss_overwrites_a_would_be_hit(self, store):
        ms, conn = store
        from alpha_agents.data import portfolio as pf
        pid = self._seed(ms, conn, return_pct=-8.0)
        with patch.object(pf, "_get_conn", lambda: conn), \
             patch.object(pf, "_write_lock", ms._write_lock):
            pf._feed_close_to_learning(1, -8.0, "止损触发")
        row = conn.execute("SELECT hit, week_return, review_note FROM predictions "
                           "WHERE id = ?", (pid,)).fetchone()
        assert row["hit"] == 0
        assert row["week_return"] == -8.0
        assert "止损触发" in row["review_note"]

    def test_profit_records_a_hit(self, store):
        ms, conn = store
        from alpha_agents.data import portfolio as pf
        pid = self._seed(ms, conn, return_pct=12.0)
        with patch.object(pf, "_get_conn", lambda: conn), \
             patch.object(pf, "_write_lock", ms._write_lock):
            pf._feed_close_to_learning(1, 12.0, "止盈触发")
        row = conn.execute("SELECT hit, week_return FROM predictions WHERE id = ?",
                           (pid,)).fetchone()
        assert row["hit"] == 1
        assert row["week_return"] == 12.0

    def test_missing_position_is_silent(self, store):
        ms, conn = store
        from alpha_agents.data import portfolio as pf
        with patch.object(pf, "_get_conn", lambda: conn), \
             patch.object(pf, "_write_lock", ms._write_lock):
            pf._feed_close_to_learning(999, 5.0, "止盈触发")  # must not raise

    def test_no_matching_prediction_is_silent(self, store):
        ms, conn = store
        from alpha_agents.data import portfolio as pf
        conn.execute(
            "INSERT INTO virtual_portfolio (id, code, name, theme, order_date, "
            "open_date, open_price, shares, status) "
            "VALUES (2, '999999', 'X', 'T', '2026-09-01', '2026-09-01', 10.0, 100, 'closed')"
        )
        conn.commit()
        with patch.object(pf, "_get_conn", lambda: conn), \
             patch.object(pf, "_write_lock", ms._write_lock):
            pf._feed_close_to_learning(2, 5.0, "止盈触发")  # must not raise
