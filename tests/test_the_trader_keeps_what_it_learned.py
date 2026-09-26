"""A replay's notes are the trader's notes, and they survive the run.

The isolation a replay runs under protects the **book**: a simulated fill
must never touch a real position. It was applied to everything the replay
wrote, including the notes it made about its own closed trades, and those
live in a scratch directory that is deleted when the run ends.

Measured 2026-09-22: a 20-day replay wrote five observations, each citing
its evidence episodes, and all five died with the temp directory. No
transfer path existed anywhere; walk_forward stated it as the design —
"historical replay screens candidates and never promotes them".

Screening a *rule* is what the promotion gate is for and that gate is
untouched. What died was the trader's recollection of what happened to it.
A trader that forgets every run cannot get better at anything, and
accumulating experience is the reason to run a replay at all.

The merged notes read as the trader's own. ``source`` is kept as
bookkeeping and ``journal.own_trade_notes`` never renders it: the agent
sees a date and what it observed, which is what a journal is.
"""

import sqlite3
from unittest.mock import patch

import pytest

from alpha_agents.evolution import journal_merge


def _replay_db(tmp_path, rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from alpha_agents.data import learning_candidates as LC
    LC.init_schema(conn)
    conn.executemany(
        "INSERT INTO learning_candidates "
        "(fingerprint, entity_type, operation, target_id, source, "
        " source_date, payload_json, claim, applicable_context, "
        " proposed_behavior_delta, evidence_episode_ids, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return tmp_path


def _note(fp, claim, status="observation"):
    return (fp, "principle", "create", None, "walk_forward_replay",
            "2026-01-30", "{}", claim, "", "{}",
            '{"supporting": [5], "opposing": []}', status, "2026-01-30")


@pytest.fixture
def durable(tmp_path, monkeypatch):
    path = tmp_path / "prod.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from alpha_agents.data import learning_candidates as LC
    LC.init_schema(conn)
    conn.commit()
    monkeypatch.setattr("alpha_agents.data.memory_store._get_conn",
                        lambda: conn)
    return conn


class TestTheNotesSurvive:
    def test_observations_reach_the_durable_journal(self, tmp_path, durable):
        src = _replay_db(tmp_path / "replay", [
            _note("a", "T-1 涨得更多的候选，实际收益更差（n=9）"),
            _note("b", "推翻自己的失效条件后又跌了 6%"),
        ])
        got = journal_merge.merge_from(src)
        assert got == {"merged": 2, "seen": 2}
        rows = durable.execute("SELECT claim FROM learning_candidates").fetchall()
        assert len(rows) == 2

    def test_rerunning_the_same_window_remembers_once(self, tmp_path, durable):
        """A second pass is the same memory, not a second memory."""
        src = _replay_db(tmp_path / "replay", [_note("a", "同一条观察")])
        assert journal_merge.merge_from(src)["merged"] == 1
        assert journal_merge.merge_from(src)["merged"] == 0
        assert durable.execute(
            "SELECT COUNT(*) FROM learning_candidates").fetchone()[0] == 1

    def test_only_observations_move(self, tmp_path, durable):
        """A row someone already advanced is not re-proposed by a rerun."""
        src = _replay_db(tmp_path / "replay", [
            _note("a", "观察"), _note("b", "已退役", status="retired")])
        assert journal_merge.merge_from(src)["seen"] == 1

    def test_nothing_arrives_already_promoted(self, tmp_path, durable):
        """Merging is remembering, not deciding."""
        src = _replay_db(tmp_path / "replay", [_note("a", "观察")])
        journal_merge.merge_from(src)
        statuses = {r[0] for r in durable.execute(
            "SELECT status FROM learning_candidates").fetchall()}
        assert statuses == {"observation"}


class TestAFailureCostsTheNotesNotTheRun:
    def test_a_replay_with_no_database(self, tmp_path, durable):
        got = journal_merge.merge_from(tmp_path / "missing")
        assert got["merged"] == 0 and "no replay database" in got["why"]

    def test_a_replay_that_learned_nothing(self, tmp_path, durable):
        src = _replay_db(tmp_path / "replay", [])
        assert journal_merge.merge_from(src) == {"merged": 0, "seen": 0}

    def test_an_unreadable_database_is_reported_not_raised(self, tmp_path,
                                                           durable, caplog):
        src = _replay_db(tmp_path / "replay", [_note("a", "x")])
        with patch.object(journal_merge.sqlite3, "connect",
                          side_effect=sqlite3.DatabaseError("locked")), \
             caplog.at_level("WARNING"):
            got = journal_merge.merge_from(src)
        assert got["merged"] == 0
        assert "cannot read" in caplog.text


class TestTheAgentReadsItAsItsOwn:
    def test_the_journal_never_renders_the_source(self, tmp_path, durable):
        src = _replay_db(tmp_path / "replay", [_note("a", "一条观察")])
        journal_merge.merge_from(src)
        from alpha_agents.evolution.journal import own_trade_notes
        text = own_trade_notes()
        assert "一条观察" in text
        assert "walk_forward_replay" not in text, (
            "the trader is reading its own experience, not a provenance tag")

    def test_the_replay_keeps_notes_by_default(self):
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        src = inspect.getsource(wf.main)
        assert "merge_from(_REPLAY_DIR)" in src
        assert "if not args.no_keep_notes" in src, "opt out, not opt in"

    def test_the_merge_runs_after_the_isolation_check(self):
        """production_untouched proves the fills touched nothing real.
        Merging inside the run would have destroyed that proof."""
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        src = inspect.getsource(wf.main)
        assert src.index("production_untouched") < src.index("merge_from")
        assert "merge_from" not in inspect.getsource(wf.run)
