"""Scheduled tasks' reports reach the report store.

Only the monitor loop ever called save_report, so the four reports of the
day that matter most — morning scan, review, night scan, weekly — existed
solely as a push notification and a 2000-char activity row. The dashboard's
report page could not show any of them, which read as "the system produced
nothing today".
"""

import sqlite3
import time

import pytest

from alpha_agents.data import report_store


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
