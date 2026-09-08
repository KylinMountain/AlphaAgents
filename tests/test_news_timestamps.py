"""RSS timestamps ending in a zone *name* must parse.

``%z`` matches numeric offsets and 'Z', not 'GMT'. Since save_news started
normalising, every feed sending RFC 2822 with an alphabetic zone — BBC,
CNBC, Bloomberg, France24, Google News, Politico — parsed to '' and had its
rows dropped on write. The fetch succeeded, the news was thrown away, and
nothing logged it: those six sources had no rows newer than the day
normalisation shipped.
"""

import pytest

from alpha_agents.data import snapshot_store
from alpha_agents.data.snapshot_store import normalise_published_at


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_store, "SNAPSHOTS_DB_PATH", tmp_path / "snap.db")
    monkeypatch.setattr(snapshot_store._local, "conn", None, raising=False)
    yield snapshot_store
    conn = getattr(snapshot_store._local, "conn", None)
    if conn is not None:
        conn.close()
    snapshot_store._local.conn = None


class TestNormalise:
    @pytest.mark.parametrize("raw", [
        "Fri, 04 Sep 2026 05:09:02 GMT",
        "Fri, 04 Sep 2026 05:09:02 UT",
        "Fri, 04 Sep 2026 05:09:02 +0000",
    ])
    def test_rfc2822_zone_spellings_all_parse(self, raw):
        assert normalise_published_at(raw) != ""

    def test_offset_is_converted_not_dropped(self):
        """Two spellings of the same instant must land on the same value."""
        gmt = normalise_published_at("Fri, 04 Sep 2026 05:09:02 GMT")
        numeric = normalise_published_at("Fri, 04 Sep 2026 05:09:02 +0000")
        assert gmt == numeric

    def test_plain_formats_still_work(self):
        assert normalise_published_at("2026-09-08 09:30:00") == "2026-09-08 09:30:00"
        assert normalise_published_at("2026-09-08") == "2026-09-08 00:00:00"

    def test_unparseable_returns_empty(self):
        assert normalise_published_at("bad input") == ""
        assert normalise_published_at("") == ""
        assert normalise_published_at(None) == ""


class TestSaveKeepsRssRows:
    def test_gmt_stamped_item_is_persisted(self, store):
        n = store.save_news("BBC Business", [{
            "title": "A headline", "summary": "body",
            "time": "Fri, 04 Sep 2026 05:09:02 GMT", "url": "",
        }])
        assert n == 1

        rows = store.read_latest_news(limit=5)
        assert [r["title"] for r in rows] == ["A headline"]


class TestMigration:
    def test_rewrites_legacy_rows_and_is_idempotent(self, store):
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO news_items (source, published_at, title, summary, url, "
            "hash, captured_at) VALUES (?,?,?,?,?,?,?)",
            ("BBC World", "Wed, 26 Aug 2026 23:03:46 GMT", "old", "", "",
             "h1", "2026-08-27 07:00"),
        )
        conn.commit()

        assert store.migrate_news_timestamps() == 1
        assert store.migrate_news_timestamps() == 0

        got = conn.execute(
            "SELECT published_at FROM news_items WHERE hash = 'h1'"
        ).fetchone()["published_at"]
        assert got.startswith("2026-08-2")

    def test_unparseable_row_is_kept_not_deleted(self, store):
        """Losing news to a migration is worse than mis-ordering it."""
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO news_items (source, published_at, title, summary, url, "
            "hash, captured_at) VALUES (?,?,?,?,?,?,?)",
            ("Weird", "sometime last tuesday", "x", "", "", "h2", "2026-08-27 07:00"),
        )
        conn.commit()

        store.migrate_news_timestamps()

        assert conn.execute(
            "SELECT COUNT(*) FROM news_items WHERE hash = 'h2'"
        ).fetchone()[0] == 1


class TestReadIgnoresLegacyOrdering:
    def test_legacy_row_cannot_take_over_the_top_of_the_feed(self, store):
        """'Wed, 26 Aug' sorts above '2026-09-08' on the first character."""
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO news_items (source, published_at, title, summary, url, "
            "hash, captured_at) VALUES (?,?,?,?,?,?,?)",
            ("BBC World", "Wed, 26 Aug 2026 23:03:46 GMT", "stale", "", "",
             "h3", "2026-08-27 07:00"),
        )
        conn.commit()
        store.save_news("财联社电报", [{
            "title": "今天的快讯", "summary": "",
            "time": "2026-09-08 09:30:00", "url": "",
        }])

        rows = store.read_latest_news(limit=5)

        assert rows[0]["title"] == "今天的快讯"
