"""The dashboard stream shows news, not scheduler plumbing.

It used to render task lifecycle rows: three per five-minute tick, which
filled all 80 visible rows with "开始 news_ingest / 完成 / 失败" and pushed
the news off the panel. Two behaviours keep it that way.
"""

from datetime import datetime, timedelta

import pytest

from alpha_agents.data import snapshot_store
from alpha_agents.pipeline.scheduler import Task, _is_high_frequency


@pytest.fixture()
def news_db(tmp_path, monkeypatch):
    """A snapshot store backed by a throwaway file."""
    monkeypatch.setattr(snapshot_store, "SNAPSHOTS_DB_PATH", tmp_path / "snap.db")
    monkeypatch.setattr(snapshot_store._local, "conn", None, raising=False)
    yield
    conn = getattr(snapshot_store._local, "conn", None)
    if conn is not None:
        conn.close()
    snapshot_store._local.conn = None


class TestReadLatestNews:
    def test_newest_first(self, news_db):
        base = datetime(2026, 9, 8, 9, 0, 0)
        snapshot_store.save_news("财联社电报", [
            {"title": f"flash {i}", "summary": "",
             "time": (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
             "link": ""}
            for i in range(5)
        ])

        rows = snapshot_store.read_latest_news(limit=3)

        assert [r["title"] for r in rows] == ["flash 4", "flash 3", "flash 2"]

    def test_flash_stamped_ahead_of_our_clock_still_shows(self, news_db):
        """The reason this is not read_news(as_of=now).

        Source clocks run a little ahead of ours. An as_of cut would drop
        exactly the newest flash — the one the panel exists to show.
        """
        ahead = (datetime.now() + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
        snapshot_store.save_news("新浪7x24", [
            {"title": "刚刚发生", "summary": "", "time": ahead, "link": ""},
        ])

        titles = [r["title"] for r in snapshot_store.read_latest_news(limit=10)]

        assert "刚刚发生" in titles

    def test_source_filter(self, news_db):
        now = datetime(2026, 9, 8, 9, 0, 0).strftime("%Y-%m-%d %H:%M:%S")
        snapshot_store.save_news("财联社电报",
                                 [{"title": "a", "summary": "", "time": now, "link": ""}])
        snapshot_store.save_news("金十数据",
                                 [{"title": "b", "summary": "", "time": now, "link": ""}])

        rows = snapshot_store.read_latest_news(limit=10, sources=["金十数据"])

        assert [r["title"] for r in rows] == ["b"]


class TestHighFrequencySuppression:
    def test_five_minute_task_is_chatty(self):
        from datetime import time as dtime
        task = Task("news_ingest", lambda: None, dtime(0, 0),
                    end_at=dtime(23, 59), interval_minutes=5)
        assert _is_high_frequency(task)

    def test_daily_report_is_not(self):
        from datetime import time as dtime
        task = Task("morning_scan", lambda: None, dtime(9, 0))
        assert not _is_high_frequency(task)

    def test_hourly_task_is_not(self):
        from datetime import time as dtime
        task = Task("hourly", lambda: None, dtime(0, 0),
                    end_at=dtime(23, 59), interval_minutes=60)
        assert not _is_high_frequency(task)
