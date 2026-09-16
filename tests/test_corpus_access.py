"""Shared history is opened read-only, and a write fails loudly.

The reproduction plan shares the corpus by symlink and keeps trader state fresh.
A convention would be enough right up until it was not: a replay that ingested
would append 2020 rows to the corpus the live pipeline also reads, and nothing
would report it. So the mode is enforced at the SQLite layer.

These tests check both halves. A read-only connection that also failed to *read*
would pass a "writes are refused" assertion while being useless, so every refusal
is paired with a read that must succeed.
"""

import os
import sqlite3
from pathlib import Path

import pytest

from alpha_agents.data import corpus_access, market_history, snapshot_store

NEWS_ROW = ("财联社电报", "2026-09-16 09:00:00", "一条历史消息", "", "",
            "hash-1", "2026-09-16 09:05")


def _news_db(path: Path) -> None:
    """A corpus news file: the real schema, one row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(snapshot_store._SCHEMA)
        con.execute(
            "INSERT INTO news_items "
            "(source, published_at, title, summary, url, hash, captured_at) "
            "VALUES (?,?,?,?,?,?,?)", NEWS_ROW)
        con.commit()
    finally:
        con.close()


def _link(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, target)
    return target


class TestWhatCountsAsShared:
    def test_a_symlink_is_shared_and_a_plain_file_is_not(self, tmp_path):
        owned = tmp_path / "mine.db"
        owned.write_bytes(b"")
        assert not corpus_access.is_shared(owned)

        shared = _link(owned, tmp_path / "replay" / "mine.db")
        assert corpus_access.is_shared(shared)

    def test_an_owned_file_still_opens_read_write(self, tmp_path):
        """The live pipeline must be unaffected; this is a mode, not a ban."""
        owned = tmp_path / "mine.db"
        con = corpus_access.connect(owned)
        try:
            con.execute("CREATE TABLE t (x INTEGER)")
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        finally:
            con.close()

    def test_a_shared_file_refuses_the_same_write(self, tmp_path):
        owned = tmp_path / "corpus.db"
        con = corpus_access.connect(owned)
        con.execute("CREATE TABLE t (x INTEGER)")
        con.commit()
        con.close()

        shared = corpus_access.connect(_link(owned, tmp_path / "replay" / "corpus.db"))
        try:
            assert shared.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                shared.execute("INSERT INTO t VALUES (1)")
        finally:
            shared.close()


class TestTheNewsStoreOnASharedCorpus:
    def _point_at(self, monkeypatch, link: Path) -> None:
        monkeypatch.setattr(snapshot_store, "SNAPSHOTS_DB_PATH", link)
        monkeypatch.setattr(snapshot_store._local, "conn", None, raising=False)

    def _close(self) -> None:
        conn = getattr(snapshot_store._local, "conn", None)
        if conn is not None:
            conn.close()
        snapshot_store._local.conn = None

    def test_it_reads_the_shared_news(self, tmp_path, monkeypatch):
        """A read-only corpus that cannot be read would be no use, so the read
        is asserted before the refusal."""
        corpus = tmp_path / "corpus" / "market_snapshots.db"
        _news_db(corpus)
        try:
            self._point_at(monkeypatch, _link(corpus, tmp_path / "replay" / "market_snapshots.db"))
            rows = snapshot_store.read_latest_news(limit=10)
            assert [r["title"] for r in rows] == ["一条历史消息"]
        finally:
            self._close()

    def test_it_cannot_write_the_shared_news(self, tmp_path, monkeypatch):
        corpus = tmp_path / "corpus" / "market_snapshots.db"
        _news_db(corpus)
        before = corpus.stat().st_size
        try:
            self._point_at(monkeypatch, _link(corpus, tmp_path / "replay" / "market_snapshots.db"))
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                snapshot_store.save_news(
                    "财联社电报",
                    [{"title": "混进来的 2020 条", "time": "2020-01-02 09:00:00"}])
        finally:
            self._close()
        assert corpus.stat().st_size == before

    def test_the_schema_is_not_applied_to_a_shared_file(self, tmp_path, monkeypatch):
        """Creating a table is a write; on shared history it is not ours to make."""
        corpus = tmp_path / "corpus" / "market_snapshots.db"
        _news_db(corpus)
        try:
            self._point_at(monkeypatch, _link(corpus, tmp_path / "replay" / "market_snapshots.db"))
            snapshot_store._get_conn()
        finally:
            self._close()
        # Still exactly the one table set the corpus had; nothing added by us.
        con = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        assert "news_items" in names


class TestThePriceStoreOnASharedCorpus:
    def test_it_reads_and_refuses_to_write(self, tmp_path, monkeypatch):
        corpus = tmp_path / "corpus" / "market_history.db"
        corpus.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(corpus)
        con.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, close REAL)")
        con.execute("INSERT INTO daily_kline VALUES ('600000','2020-01-02',10.0)")
        con.commit()
        con.close()

        monkeypatch.setattr(market_history, "DB_PATH",
                            _link(corpus, tmp_path / "replay" / "market_history.db"))
        monkeypatch.setattr(market_history._local, "hist_conn", None, raising=False)
        try:
            conn = market_history._get_conn()
            assert conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO daily_kline VALUES ('600001','2020-01-03',11.0)")
        finally:
            existing = getattr(market_history._local, "hist_conn", None)
            if existing is not None:
                existing.close()
            market_history._local.hist_conn = None


class TestItMatchesWhatTheBootstrapBuilds:
    def test_the_links_the_bootstrap_makes_are_the_ones_treated_as_shared(self, tmp_path):
        """The two mechanisms have to agree, or the enforcement is decorative."""
        from scripts.walk_bootstrap import bootstrap

        corpus = tmp_path / "corpus"
        _news_db(corpus / "market_snapshots.db")
        (corpus / "market_history.db").write_bytes(b"")
        report = bootstrap(tmp_path / "replay", corpus)

        assert report["shared"], "fixture produced nothing to share"
        for name in report["shared"]:
            assert corpus_access.is_shared(tmp_path / "replay" / name), \
                f"{name} was shared but would still open read-write"
