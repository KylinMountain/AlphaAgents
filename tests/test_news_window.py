"""Timestamp normalisation and windowed reads over the news store."""

from datetime import datetime

import pytest

from alpha_agents.data.snapshot_store import normalise_published_at


class TestNormalisePublishedAt:
    """Sources disagree on format; the store must not.

    Window queries compare published_at as a *string*, so a mixed table
    silently returns the wrong rows.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("2026-09-07 22:28:19", "2026-09-07 22:28:19"),
        ("2026-09-07 22:28", "2026-09-07 22:28:00"),
        ("2026-09-07T22:28:19", "2026-09-07 22:28:19"),
        ("2026-09-07", "2026-09-07 00:00:00"),
    ])
    def test_iso_variants(self, raw, expected):
        assert normalise_published_at(raw) == expected

    def test_rfc2822_from_rss(self):
        # RSS feeds send 'Mon, 07 Sep 2026 …' — lexically this sorts under
        # "M", nowhere near a 2026-… row.
        assert normalise_published_at("Mon, 07 Sep 2026") == "2026-09-07 00:00:00"
        assert (normalise_published_at("Mon, 07 Sep 2026 14:30:00")
                == "2026-09-07 14:30:00")

    def test_yearless_format_assumes_current_year(self):
        got = normalise_published_at("Fri Oct 28 03:49")
        assert got.startswith(f"{datetime.now().year}-10-28")

    def test_unparseable_returns_empty_so_caller_can_drop(self):
        for raw in ("", "   ", "昨天", "not a date", None):
            assert normalise_published_at(raw) == ""

    def test_output_is_lexically_ordered(self):
        """The whole point: string sort must equal chronological sort."""
        raws = ["Mon, 07 Sep 2026 14:30:00", "2026-09-07", "2026-09-08 09:00",
                "2026-09-06 23:59:59"]
        got = sorted(normalise_published_at(r) for r in raws)
        assert got == [
            "2026-09-06 23:59:59",
            "2026-09-07 00:00:00",
            "2026-09-07 14:30:00",
            "2026-09-08 09:00:00",
        ]


@pytest.fixture
def store(tmp_path, monkeypatch):
    import sqlite3
    from alpha_agents.data import snapshot_store as ss

    conn = sqlite3.connect(str(tmp_path / "snap.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(ss._SCHEMA)
    conn.commit()
    monkeypatch.setattr(ss, "_get_conn", lambda: conn)
    yield ss
    conn.close()


class TestWindowedRead:
    def _seed(self, ss):
        ss.save_news("金十数据", [
            {"title": "旧闻", "summary": "s", "time": "2026-09-06 10:00:00"},
            {"title": "昨夜", "summary": "s", "time": "2026-09-07 02:00:00"},
            {"title": "今早", "summary": "s", "time": "2026-09-07 08:00:00"},
            {"title": "刚刚", "summary": "s", "time": "2026-09-07 09:30:00"},
        ])

    def test_since_bounds_the_window_below(self, store):
        self._seed(store)
        got = store.read_news(None, "2026-09-07 10:00:00",
                              since="2026-09-07 06:00:00")
        assert {g["title"] for g in got} == {"今早", "刚刚"}

    def test_as_of_still_bounds_above(self, store):
        self._seed(store)
        got = store.read_news(None, "2026-09-07 03:00:00",
                              since="2026-09-06 00:00:00")
        assert {g["title"] for g in got} == {"旧闻", "昨夜"}

    def test_without_since_behaviour_is_unchanged(self, store):
        self._seed(store)
        got = store.read_news(None, "2026-09-07 10:00:00")
        assert len(got) == 4

    def test_date_only_since_expands_to_midnight(self, store):
        self._seed(store)
        got = store.read_news(None, "2026-09-07 23:59:59", since="2026-09-07")
        assert {g["title"] for g in got} == {"昨夜", "今早", "刚刚"}

    def test_rfc2822_input_lands_in_the_window(self, store):
        """An RSS item must be findable by an ISO window."""
        store.save_news("BBC", [
            {"title": "RSS 条目", "summary": "s",
             "time": "Mon, 07 Sep 2026 08:30:00"},
        ])
        got = store.read_news(["BBC"], "2026-09-07 10:00:00",
                              since="2026-09-07 06:00:00")
        assert [g["title"] for g in got] == ["RSS 条目"]

    def test_unparseable_timestamp_is_not_stored(self, store):
        n = store.save_news("X", [{"title": "坏时间", "summary": "s",
                                   "time": "前天下午"}])
        assert n == 0

    def test_repeat_save_is_deduped(self, store):
        self._seed(store)
        self._seed(store)
        got = store.read_news(None, "2026-09-07 23:59:59")
        assert len(got) == 4
