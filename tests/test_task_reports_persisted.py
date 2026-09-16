"""Scheduled tasks' reports reach the report store.

Only the monitor loop ever called save_report, so the four reports of the
day that matter most — morning scan, review, night scan, weekly — existed
solely as a push notification and a 2000-char activity row. The dashboard's
report page could not show any of them, which read as "the system produced
nothing today".

The same defect, one table over: ``reviews`` had exactly one writer, and it
was the *manual* CLI (``pipeline/daily_review.py``). The scheduled 15:30
review built a report, pushed it to the phone, and archived nothing — so
the Memory page's 复盘记录 card printed "表在 · 0 行" every day while a
review was in fact being produced. See
``TestTheScheduledReviewArchivesItself``.
"""

import asyncio
import json
import sqlite3
import time
from datetime import datetime

import pytest

from alpha_agents.data import memory_store, report_store
from alpha_agents.pipeline.tasks import review as R


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(report_store, "REPORTS_DB_PATH", tmp_path / "reports.db")
    monkeypatch.setattr(report_store._local, "conn", None, raising=False)
    yield report_store
    conn = getattr(report_store._local, "conn", None)
    if conn is not None:
        conn.close()
    report_store._local.conn = None


class TestSaveTaskReport:
    def test_report_is_readable_back(self, store):
        store.save_task_report("morning_scan", "=== 晨报 ===\n主线: AI 算力")

        rows = store.get_recent_reports(5)

        assert len(rows) == 1
        assert rows[0]["report_type"] == "morning_scan"
        assert "AI 算力" in rows[0]["report_text"]

    def test_cycle_stays_null(self, store):
        """A task report is not a monitor cycle; a made-up number collides."""
        store.save_task_report("review", "复盘正文")

        assert store.get_recent_reports(1)[0]["cycle"] is None

    def test_monitor_reports_still_work_alongside(self, store):
        store.save_report(7, time.time(), [{"title": "e"}], {"宏观": 1}, "追因正文")
        store.save_task_report("night_scan", "夜扫正文")

        rows = store.get_recent_reports(5)
        by_type = {r["report_type"]: r for r in rows}

        assert by_type["night_scan"]["cycle"] is None
        assert by_type[None]["cycle"] == 7


class TestMigration:
    def test_column_is_added_to_an_older_database(self, tmp_path, monkeypatch):
        """CREATE TABLE IF NOT EXISTS never alters a table that exists."""
        db = tmp_path / "reports.db"
        old = sqlite3.connect(db)
        old.execute(
            "CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "cycle INTEGER, timestamp REAL NOT NULL, "
            "event_count INTEGER NOT NULL DEFAULT 0, categories TEXT, "
            "events_json TEXT, report_text TEXT, "
            "created_at TEXT DEFAULT (datetime('now')))"
        )
        old.execute(
            "INSERT INTO reports (cycle, timestamp, report_text) VALUES (1, ?, ?)",
            (time.time(), "旧报告"),
        )
        old.commit()
        old.close()

        monkeypatch.setattr(report_store, "REPORTS_DB_PATH", db)
        monkeypatch.setattr(report_store._local, "conn", None, raising=False)
        try:
            rows = report_store.get_recent_reports(5)
            assert rows[0]["report_type"] is None, "old row keeps a null type"
            assert rows[0]["report_text"] == "旧报告", "no data lost"

            report_store.save_task_report("morning_scan", "新报告")
            types = {r["report_type"] for r in report_store.get_recent_reports(5)}
            assert types == {None, "morning_scan"}
        finally:
            conn = getattr(report_store._local, "conn", None)
            if conn is not None:
                conn.close()
            report_store._local.conn = None


# ── The 复盘记录 table, and who writes it ──────────────────────────────


@pytest.fixture()
def both_stores(tmp_path, monkeypatch):
    """Both stores on tmp paths, and every outbound call stubbed out.

    ``run_review`` is the scheduled 15:30 task: it reads market data, calls
    the review agent and pushes a notification. None of that belongs in a
    test about persistence, and none of it is reachable from a sandbox — so
    the seams are cut here and every assertion below is about what got
    *written*, not about what the agent said.
    """
    monkeypatch.setattr(report_store, "REPORTS_DB_PATH",
                        tmp_path / "reports.db")
    monkeypatch.setattr(report_store._local, "conn", None, raising=False)
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)

    # Outbound, LLM, or notification: never called.
    monkeypatch.setattr(R, "_score_due_predictions", lambda: "")
    monkeypatch.setattr(R, "_label_outcomes", lambda today: {})
    monkeypatch.setattr(R, "_update_themes_from_market_data", lambda t: None)
    monkeypatch.setattr(R, "_update_market_cognition", lambda t, d: None)
    monkeypatch.setattr(R, "refresh_theme_scores", lambda: 0)
    monkeypatch.setattr(R, "retire_stale_themes", lambda: [])
    monkeypatch.setattr(R, "notify_all", lambda *a, **k: None)
    monkeypatch.setattr(R, "run_daily_archive", lambda: None)

    async def _report(*_a, **_k):
        return "复盘正文"

    monkeypatch.setattr(R, "run_review_analysis", _report)

    # These three are imported *inside* `run_review`, so they are patched at
    # the source module rather than on `R`.
    import alpha_agents.data.market_history as mh
    import alpha_agents.data.sentiment_cycle as sc
    import alpha_agents.evolution as ev

    monkeypatch.setattr(mh, "update_daily", lambda: 0)
    monkeypatch.setattr(sc, "compute_and_save_sentiment", lambda: {})

    async def _no_evolution(*_a, **_k):
        return ""

    monkeypatch.setattr(ev, "post_review", _no_evolution)

    yield report_store
    for mod in (report_store, memory_store):
        conn = getattr(mod._local, "conn", None)
        if conn is not None:
            conn.close()
        mod._local.conn = None


class TestTheScheduledReviewArchivesItself:
    def test_the_task_writes_the_row_the_memory_page_reads(
            self, both_stores, monkeypatch):
        """The whole point: after the scheduled task runs, the table the
        page reads has a row in it. It had none for as long as the task
        existed, because the only caller of ``save_review`` was a CLI."""
        monkeypatch.setattr(R, "_verify_today_predictions", lambda p: {
            "text": "| 代码 |\n|--|", "total": 4, "hits": 3, "neutral": 1,
            "unreachable": 0, "unique": 5, "accuracy": 0.75})

        assert both_stores.get_recent_reviews(5) == [], "empty to begin with"

        asyncio.run(R.run_review())

        rows = both_stores.get_recent_reviews(5)
        assert len(rows) == 1, rows
        assert rows[0]["date"] == datetime.now().strftime("%Y-%m-%d")
        assert rows[0]["predictions_count"] == 4
        assert rows[0]["correct_count"] == 3
        assert rows[0]["accuracy"] == 0.75
        assert "复盘正文" in rows[0]["review_text"]

    def test_the_archived_row_explains_its_own_denominator(
            self, both_stores, monkeypatch):
        """``predictions_count`` is the hit-rate denominator, which is not
        the number of rows the pass looked at: limit-up rows were never
        tradeable and sit outside it. A row that carried only the smaller
        number would make the exclusion invisible."""
        monkeypatch.setattr(R, "_verify_today_predictions", lambda p: {
            "text": "t", "total": 4, "hits": 3, "neutral": 1,
            "unreachable": 2, "unique": 7, "accuracy": 0.75})

        asyncio.run(R.run_review())

        md = json.loads(both_stores.get_recent_reviews(1)[0]["market_data"])
        assert md["source"] == "scheduled_review"
        assert md["unreachable"] == 2
        assert md["unique"] == 7


class TestTheVerificationReturnsItsCounts:
    """``_verify_today_predictions`` used to return only the rendered table,
    so the counts it computed lived for one expression and were dropped.
    That is *why* the task could print a hit rate and archive nothing."""

    def _run(self, monkeypatch, quotes):
        import alpha_agents.data.market_data as md
        monkeypatch.setattr(md, "get_realtime_quotes", lambda codes: quotes)
        return R._verify_today_predictions([
            {"code": "600000", "name": "浦发", "direction": "bullish",
             "entry_price": 10.0, "theme_line": "金融"},
            {"code": "600001", "name": "邯郸", "direction": "bullish",
             "entry_price": 10.0, "theme_line": "钢铁"},
            {"code": "600002", "name": "包钢", "direction": "bullish",
             "entry_price": 10.0, "theme_line": "钢铁"},
            {"code": "600003", "name": "一字板", "direction": "bullish",
             "entry_price": 10.0, "theme_line": "钢铁"},
        ])

    def test_hits_and_the_denominator_come_back(self, monkeypatch):
        v = self._run(monkeypatch, {
            "600000": {"price": 10.5, "change_pct": 5.0},    # +5%  → 命中
            "600001": {"price": 9.5, "change_pct": -5.0},    # -5%  → 未命中
            "600002": {"price": 10.0, "change_pct": 0.0},    # 0%   → 中性
            "600003": {"price": 11.0, "change_pct": 10.0},   # +10% → 涨停未入场
        })
        assert v["unique"] == 4
        assert v["unreachable"] == 1, "涨停那只不在分母里"
        assert v["total"] == 3, "分母是实际可交易的三只"
        assert v["hits"] == 1
        assert v["neutral"] == 1
        assert v["accuracy"] == round(1 / 3, 4)
        assert "命中率: 1/3" in v["text"]

    def test_no_predictions_returns_the_same_shape(self):
        """The caller reads keys off this, so the empty case cannot be a
        bare string — that is how the counts went missing in the first
        place."""
        v = R._verify_today_predictions([])
        assert v["text"] == "今日无待验证预测"
        assert v["total"] == 0 and v["hits"] == 0 and v["accuracy"] == 0.0
