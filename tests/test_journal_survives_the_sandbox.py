"""The trader's memory does not live in the sandbox its fills live in.

A replay swaps ``DATA_DIR`` for a scratch directory so a simulated fill can
never touch a real position. The memory was swapped with it, so every run
began from nothing — journal_merge made the notes durable and nothing read
them back, which closes replay→production and leaves replay→replay open.
A trader that starts from zero on every run accumulates nothing, and
accumulating is the point.

``as_of`` is what keeps reading both honest. A replay of 2026-01 must not
be handed a note written in 2026-09: that note is derived from the future's
own answers, and no amount of "it is the same trader" makes it fair. The
filter applies to every source, which is why reading two is safe.

**A risk this does not remove, stated because it is real.** Replaying one
window repeatedly accumulates notes about those exact days, and a trader
that reads them is closer to memorising the window than to learning from
it. The notes carry their own n and are never promoted to rules by this
path, but the exposure is there and belongs in the operator's head, not
in a comment nobody reads.
"""

import sqlite3

import pytest

from alpha_agents.evolution import journal


def _seed(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from alpha_agents.data import learning_candidates as LC
    LC.init_schema(conn)
    conn.executemany(
        "INSERT INTO learning_candidates "
        "(fingerprint, entity_type, operation, target_id, source, "
        " source_date, payload_json, claim, applicable_context, "
        " proposed_behavior_delta, evidence_episode_ids, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(fp, "principle", "create", None, "x", date, "{}", claim, "", "{}",
          '{"supporting": [], "opposing": []}', "observation", date)
         for fp, date, claim in rows])
    conn.commit()
    conn.close()


@pytest.fixture
def durable(tmp_path):
    """The journal a replay names. Production names nothing and reads its
    own book, which is why this is a parameter and not a derived path: a
    module that guessed would reach into the real database from every
    sandbox, including this one."""
    return tmp_path / "data" / "memory.db"


class TestTheMemoryOutlivesTheRun:
    def test_a_sandbox_still_reads_what_was_learned_before(self, durable,
                                                           monkeypatch):
        _seed(durable, [("a", "2026-01-10", "上次这条主线追高吃了亏")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [])
        assert "上次这条主线追高吃了亏" in journal.own_trade_notes("2026-01-20", durable=durable)

    def test_both_sources_are_read(self, durable, monkeypatch):
        _seed(durable, [("a", "2026-01-10", "持久库里的")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [{"fingerprint": "b", "source_date": "2026-01-12",
                              "claim": "本轮写下的",
                              "evidence_episode_ids": "{}"}])
        text = journal.own_trade_notes("2026-01-20", durable=durable)
        assert "持久库里的" in text and "本轮写下的" in text

    def test_the_same_note_is_remembered_once(self, durable, monkeypatch):
        """A merged note appears in both stores; it is one memory."""
        _seed(durable, [("same", "2026-01-10", "同一条")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [{"fingerprint": "same",
                              "source_date": "2026-01-10", "claim": "同一条",
                              "evidence_episode_ids": "{}"}])
        assert journal.own_trade_notes("2026-01-20", durable=durable).count("同一条") == 1


class TestTheFutureStaysOutOfIt:
    def test_a_note_from_after_the_day_is_not_shown(self, durable, monkeypatch):
        """The lookahead this design would otherwise have created."""
        _seed(durable, [("later", "2026-09-18", "九月才写下的结论")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [])
        assert journal.own_trade_notes("2026-01-15", durable=durable) == ""

    def test_the_filter_covers_the_active_book_too(self, durable, monkeypatch):
        _seed(durable, [])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [{"fingerprint": "f", "source_date": "2026-09-18",
                              "claim": "未来的", "evidence_episode_ids": "{}"}])
        assert journal.own_trade_notes("2026-01-15", durable=durable) == ""

    def test_no_as_of_means_every_note(self, durable, monkeypatch):
        """Production asks for today's memory with no bound."""
        _seed(durable, [("a", "2026-01-10", "旧的"), ("b", "2026-09-18", "新的")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [])
        text = journal.own_trade_notes(durable=durable)
        assert "旧的" in text and "新的" in text


class TestAFailureCostsTheMemoryNotTheDecision:
    def test_a_missing_durable_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [])
        assert journal.own_trade_notes(durable=tmp_path / "gone.db") == ""

    def test_naming_nothing_reads_only_the_active_book(self, monkeypatch):
        """Production's shape: no second store to reach into."""
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [{"fingerprint": "f", "source_date": "2026-01-10",
                              "claim": "本本上的", "evidence_episode_ids": "{}"}])
        assert "本本上的" in journal.own_trade_notes()

    def test_an_unreadable_durable_store_is_logged(self, durable, monkeypatch,
                                                   caplog):
        durable.parent.mkdir(parents=True, exist_ok=True)
        durable.write_text("not a database")
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: [])
        with caplog.at_level("WARNING"):
            assert journal.own_trade_notes(durable=durable) == ""
        assert "Durable journal unreadable" in caplog.text

    def test_a_broken_active_book_still_yields_the_durable_notes(
            self, durable, monkeypatch, caplog):
        _seed(durable, [("a", "2026-01-10", "还记得这条")])
        monkeypatch.setattr(
            "alpha_agents.data.learning_candidates.candidates_by_status",
            lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
        with caplog.at_level("WARNING"):
            assert "还记得这条" in journal.own_trade_notes("2026-01-20", durable=durable)
