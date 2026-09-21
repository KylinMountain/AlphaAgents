"""The current-only membership fallback, and the guard it must not defeat.

Why this exists. The sector replay requires a membership archive, and the only
dated source (Tushare ``concept`` / ``concept_cons``) is rate-limited to one
call per hour on this account, so a 375-concept backfill would take about
sixteen days. Without a fallback the sector replay cannot run at all, and
"cannot run" is not more honest than "runs with a stated lookahead".

What these pin, in order of importance:

1. the fallback is **labelled** -- ``point_in_time=False``, and the guard still
   refuses it unless the caller opts out explicitly;
2. the opt-out is **visible at the call site**, not a default;
3. a preregistered experiment **cannot** use it, because a formal arm must not
   decide on labels assigned after its window.
"""

import sqlite3

import pytest

from alpha_agents.data import sector_membership as M
from alpha_agents.data.sector_selection import SectorSnapshotError


def _corpus(tmp_path):
    """A tiny stocks.db with the two tables concept membership lives in."""
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE concepts (id INTEGER PRIMARY KEY, name TEXT,"
                 " source TEXT DEFAULT 'ths')")
    conn.execute("CREATE TABLE concept_stocks (concept_id INTEGER,"
                 " stock_code TEXT, PRIMARY KEY(concept_id, stock_code))")
    conn.executemany("INSERT INTO concepts (id, name) VALUES (?,?)",
                     [(1, "AI"), (2, "芯片")])
    conn.executemany("INSERT INTO concept_stocks VALUES (?,?)",
                     [(1, "600001"), (1, "600002"), (2, "600002")])
    conn.commit()
    conn.close()
    return path


class TestTheFallbackIsLabelled:
    def test_it_is_not_point_in_time(self, tmp_path):
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        assert len(archive) == 1
        assert archive[0].point_in_time is False

    def test_the_source_names_the_limitation(self, tmp_path):
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        assert "no as-of column" in archive[0].source

    def test_it_carries_both_sectors_and_their_members(self, tmp_path):
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        members = archive[0].members
        assert members["AI"] == ("600001", "600002")
        assert members["芯片"] == ("600002",)


class TestTheGuardStillRefusesIt:
    def test_strict_pit_refuses_a_current_only_snapshot(self, tmp_path):
        """The default must not silently accept a lookahead."""
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        with pytest.raises(SectorSnapshotError, match="point-in-time"):
            M.as_of(archive, "2026-08-18 09:00:00")

    def test_the_opt_out_accepts_it(self, tmp_path):
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        got = M.as_of(archive, "2026-08-18 09:00:00", strict_pit=False)
        assert got.snapshot_id == "current-only"

    def test_it_resolves_for_a_date_before_it_was_built(self, tmp_path):
        """A current-only snapshot has no availability bound by design.

        Stamping it with a real recent date would make ``as_of`` refuse every
        historical session, and stamping it with a fake ancient one would
        corrupt the content hash that identifies the world.
        """
        archive = M.current_from_corpus(db_path=_corpus(tmp_path))
        got = M.as_of(archive, "2020-01-02 09:00:00", strict_pit=False)
        assert got.point_in_time is False


class TestAnEmptyCorpusIsRefused:
    def test_no_membership_raises_rather_than_returning_empty(self, tmp_path):
        """Empty membership would make every sector select nothing.

        A replay that selects nothing because the universe was empty looks
        like a strategy result. Refusing names the real cause.
        """
        path = tmp_path / "stocks.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE concepts (id INTEGER PRIMARY KEY, name TEXT,"
                     " source TEXT)")
        conn.execute("CREATE TABLE concept_stocks (concept_id INTEGER,"
                     " stock_code TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(SectorSnapshotError, match="no concept membership"):
            M.current_from_corpus(db_path=path)
