"""The replay must not be able to inherit a trader's memory.

The external review of the T+1 plan called this its first required test:

> **State isolation**: 2026 production principle/candidate 人工塞一条，2020 replay
> 绝对检索不到；生产 DB hash 全程不变。

Merely pointing every market reader at an as-of date is not enough — the agent's
brain would still be 2026's. So the boundary is drawn at the data directory:
``memory.db`` is the trader, and a replay gets a **new** one, while the history
is shared by reference.

Each test below proves its fixture first. An "isolation" assertion that passes
because nothing was ever seeded is the sort of test this repository has already
been bitten by.
"""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.walk_bootstrap import (
    bootstrap, trader_state_rows,
)

SEEDED_CLAIM = "2026年学到的一条原则"


def _tiny_db(path: Path, ddl: str, insert: str | None = None) -> None:
    """A throwaway database. ``executescript`` for both, so a seed can be several
    statements — ``execute`` takes exactly one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        if insert:
            con.executescript(insert)
        con.commit()
    finally:
        con.close()


def _corpus(tmp_path: Path) -> Path:
    """A stand-in corpus: two files that get shared, and one seeded memory.db."""
    corpus = tmp_path / "corpus"
    _tiny_db(corpus / "market_history.db", "CREATE TABLE daily_kline (date TEXT);")
    _tiny_db(corpus / "market_snapshots.db", "CREATE TABLE news_items (id INTEGER);")
    _tiny_db(
        corpus / "memory.db",
        "CREATE TABLE learning_candidates (id INTEGER PRIMARY KEY, claim TEXT);"
        "CREATE TABLE virtual_portfolio (id INTEGER PRIMARY KEY, code TEXT);",
        f"INSERT INTO learning_candidates (claim) VALUES ('{SEEDED_CLAIM}');"
        "INSERT INTO virtual_portfolio (code) VALUES ('600000');",
    )
    return corpus


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestTheBoundaryIsReal:
    def test_state_seeded_in_the_corpus_cannot_reach_the_replay(self, tmp_path):
        """The review's test, at the level where the boundary actually lives.

        The corpus is seeded with a 2026 candidate and a 2026 position. The
        replay's state must contain neither — and the corpus file must be
        byte-identical afterwards, since sharing history is not licence to
        write it.
        """
        corpus = _corpus(tmp_path)
        seeded = trader_state_rows(corpus / "memory.db")
        assert "learning_candidates" in seeded and "virtual_portfolio" in seeded, \
            "fixture did not seed the corpus, so the isolation claim would be vacuous"
        before = _sha(corpus / "memory.db")

        report = bootstrap(tmp_path / "replay", corpus)

        assert report["state_rows"] == {}, (
            f"the replay inherited trader state: {report['state_rows']}")
        assert trader_state_rows(tmp_path / "replay" / "memory.db") == {}
        assert _sha(corpus / "memory.db") == before, \
            "the corpus memory.db was written through by the bootstrap"

    def test_the_replay_state_has_the_schema_but_no_rows(self, tmp_path):
        """Empty because nothing has happened yet, not because it is broken."""
        bootstrap(tmp_path / "replay", _corpus(tmp_path))
        con = sqlite3.connect(f"file:{tmp_path / 'replay' / 'memory.db'}?mode=ro",
                              uri=True)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        expected = {
            "virtual_portfolio", "predictions",
            "opportunity_sets", "opportunity_items",
            "theme_opportunity_sets", "theme_opportunity_items",
        }
        assert expected <= tables, (
            "the fresh state must carry every first-day review journal table")

    def test_memory_db_is_never_a_link_to_the_corpus(self, tmp_path):
        corpus = _corpus(tmp_path)
        bootstrap(tmp_path / "replay", corpus)
        assert not (tmp_path / "replay" / "memory.db").is_symlink()

    def test_history_is_shared_by_reference_rather_than_copied(self, tmp_path):
        corpus = _corpus(tmp_path)
        report = bootstrap(tmp_path / "replay", corpus)
        assert set(report["shared"]) == {"market_history.db", "market_snapshots.db"}
        assert (tmp_path / "replay" / "market_history.db").is_symlink()


class TestItRefusesRatherThanWipes:
    #: A state row the live schema makes easy to write: every column is NOT NULL
    #: but every value is a literal. (``predictions`` is not — it carries the
    #: run's own required columns, so seeding it would test the schema, not the
    #: refuse-to-clobber rule.)
    _SEED = ("INSERT INTO pending_settlements "
             "(exit_id, trader_id, code, net_amount, exit_date, settle_date) "
             "VALUES (1, 'slow', '600000', 1000.0, '2026-01-05', '2026-01-06');")

    def test_a_target_that_already_ran_is_not_replaced_by_accident(self, tmp_path):
        corpus = _corpus(tmp_path)
        target = tmp_path / "replay"
        bootstrap(target, corpus)
        # Something has now happened in this replay directory.
        _tiny_db(target / "memory.db", "SELECT 1;", self._SEED)
        assert trader_state_rows(target / "memory.db") == {"pending_settlements": 1}

        with pytest.raises(FileExistsError, match="already holds trader state"):
            bootstrap(target, corpus)

        assert trader_state_rows(target / "memory.db") == {"pending_settlements": 1}, \
            "the refused call must not have touched the existing state"

    def test_force_replaces_it(self, tmp_path):
        corpus = _corpus(tmp_path)
        target = tmp_path / "replay"
        bootstrap(target, corpus)
        _tiny_db(target / "memory.db", "SELECT 1;", self._SEED)
        report = bootstrap(target, corpus, force=True)
        assert report["state_rows"] == {}


class TestInputs:
    def test_a_missing_corpus_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="corpus directory not found"):
            bootstrap(tmp_path / "replay", tmp_path / "nope")

    def test_absent_corpus_files_are_reported_not_faked(self, tmp_path):
        """A missing stocks.db is named, not silently skipped."""
        corpus = tmp_path / "corpus"
        _tiny_db(corpus / "market_history.db", "CREATE TABLE daily_kline (date TEXT);")
        report = bootstrap(tmp_path / "replay", corpus)
        assert "market_snapshots.db" in report["missing"]
        assert not (tmp_path / "replay" / "market_snapshots.db").exists()


class TestTheDataDirOverride:
    """``ALPHAAGENTS_DATA_DIR`` is how a replay points at its own directory.

    The resolver is asked directly rather than by reloading ``config``: a reload
    would fight the suite's own storage isolation, and the question here is what
    a given environment produces, not what the module happens to hold now.
    """

    def test_the_variable_moves_every_store(self, tmp_path):
        from alpha_agents.config import PROJECT_ROOT, _data_dir

        assert _data_dir({"ALPHAAGENTS_DATA_DIR": str(tmp_path)}) == tmp_path
        assert _data_dir({}) == PROJECT_ROOT / "data"

    def test_a_blank_value_falls_back_instead_of_pointing_at_the_cwd(self):
        """``ALPHAAGENTS_DATA_DIR=`` in a shell profile must not mean ``Path('')``.

        An empty string is falsy, so the default wins — otherwise every store
        would resolve relative to whatever directory the process started in,
        which is the kind of failure that looks like data loss.
        """
        from alpha_agents.config import PROJECT_ROOT, _data_dir

        assert _data_dir({"ALPHAAGENTS_DATA_DIR": ""}) == PROJECT_ROOT / "data"
