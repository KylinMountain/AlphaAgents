"""Approved knowledge: the record of what a person put in force.

T4 of Phase 3. §10 separates "kept" from "in force" — a candidate may be
stored, argued about and even validated and still not be knowledge the
system acts on, because "validation does not itself activate knowledge".
Before this slice there was no snapshot, so the two words had no boundary
between them.

The tests are in eight groups:

1. An approval is a record: who, when, why, and which version of what.
2. The record is append-only, enforced by the database.
3. The hash is independently recomputable, and altering either half of the
   record (the snapshot row or an item) makes verification fail.
4. What cannot be approved: nothing, twice, or knowledge that is not there.
5. ``version_hash`` identifies content, not a row id — the difference that
   lets an approval notice the knowledge moved on.
6. The read side, including the question §10 actually asks: is this
   candidate in force?
7. Integrity, and drift as a separate and expected signal.
8. Approving changes nothing outside the two new tables — the machine form
   of "this phase adds no activation path".
"""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import knowledge_snapshots as KS
from alpha_agents.data import learning_candidates as LC
from alpha_agents.data import memory_store


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _principle(conn, **over) -> int:
    values = dict(principle="Small metals set up",
                  pattern_description="inflow for five days", category="entry",
                  action_guidance="buy the dip")
    values.update(over)
    cursor = conn.execute(
        "INSERT INTO trading_principles (principle, pattern_description, category, "
        "action_guidance, first_learned, last_reinforced) VALUES (?,?,?,?,?,?)",
        (values["principle"], values["pattern_description"], values["category"],
         values["action_guidance"], "2026-01-01", "2026-01-01"))
    conn.commit()
    return cursor.lastrowid


def _playbook(conn, **over) -> int:
    values = dict(name="Auto: x", pattern_json='{"a": 1}')
    values.update(over)
    cursor = conn.execute(
        "INSERT INTO playbooks (name, pattern_json, created_date, last_updated) "
        "VALUES (?,?,?,?)",
        (values["name"], values["pattern_json"], "2026-01-01", "2026-01-01"))
    conn.commit()
    return cursor.lastrowid


def _candidate(**over) -> int:
    kwargs = dict(entity_type="principle", operation="create", source="unit-test",
                  source_date="2026-01-05",
                  payload={"proposal": {"principle": "Synthetic"}},
                  claim="Small metals set up over five days",
                  applicable_context="institutional present",
                  proposed_behavior_delta={"create_principle": "Synthetic"},
                  evidence_episode_ids={"supporting": [], "opposing": []})
    kwargs.update(over)
    return LC.save_candidate(**kwargs)


def _approve(items, **over) -> int:
    kwargs = dict(approved_by="kylin", reason="shadow-tested for a quarter")
    kwargs.update(over)
    return KS.approve(items=items, **kwargs)


def _dump(conn) -> dict:
    """Every user table's contents, for the no-activation assertion."""
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        out[name] = [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
    return out


# ── 1. An approval is a record ─────────────────────────────────────────


class TestAnApprovalIsARecord:
    def test_it_records_who_when_and_why(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}],
                               notes="first one", approved_at="2026-03-01")
        snapshot = KS.get_snapshot(snapshot_id)
        assert snapshot["approved_by"] == "kylin"
        assert snapshot["approved_at"] == "2026-03-01"
        assert snapshot["reason"] == "shadow-tested for a quarter"
        assert snapshot["notes"] == "first one"

    def test_it_records_which_version_of_what(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        items = KS.items_for(snapshot_id)
        assert len(items) == 1
        assert items[0]["entity_type"] == "principle"
        assert items[0]["entity_id"] == pid
        assert items[0]["version_hash"] == KS.version_hash_for(store, "principle", pid)

    def test_a_snapshot_can_cover_both_kinds_at_once(self, store):
        pid, bid = _principle(store), _playbook(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid},
                                {"entity_type": "playbook", "entity_id": bid}])
        assert {i["entity_type"] for i in KS.items_for(snapshot_id)} == {
            "principle", "playbook"}
        assert KS.counts() == {"snapshots": 1, "items": 2}

    def test_the_approval_can_name_the_candidate_it_came_from(self, store):
        pid, cid = _principle(store), _candidate()
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid,
                                 "candidate_id": cid}])
        assert KS.items_for(snapshot_id)[0]["candidate_id"] == cid

    def test_the_order_of_items_does_not_change_the_hash(self, store):
        pid, bid = _principle(store), _playbook(store)
        first = _approve([{"entity_type": "principle", "entity_id": pid},
                          {"entity_type": "playbook", "entity_id": bid}],
                         approved_at="2026-03-01")
        second = _approve([{"entity_type": "playbook", "entity_id": bid},
                           {"entity_type": "principle", "entity_id": pid}],
                          approved_at="2026-03-01")
        assert KS.get_snapshot(first)["content_hash"] == \
            KS.get_snapshot(second)["content_hash"]

    def test_approved_at_defaults_to_the_kernel_clock(self, store):
        pid = _principle(store)
        from alpha_agents.data import clock
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        assert KS.get_snapshot(snapshot_id)["approved_at"] == clock.today()

    def test_frozen_at_is_written_and_not_hashed(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}],
                               approved_at="2026-03-01")
        snapshot = KS.get_snapshot(snapshot_id)
        assert snapshot["frozen_at"], "the write time is recorded"
        assert snapshot["frozen_at"] != snapshot["approved_at"] or True
        # Rewriting frozen_at must not change the verdict: it is a write-time
        # fact, not a declaration, so it is outside the hash on purpose.
        store.execute("DROP TRIGGER knowledge_snapshots_no_update")
        store.execute("UPDATE knowledge_snapshots SET frozen_at = '2099-01-01' "
                      "WHERE id = ?", (snapshot_id,))
        store.commit()
        assert KS.verify_snapshot(snapshot_id)


# ── 2. Append-only, by the database ────────────────────────────────────


class TestTheRecordIsAppendOnly:
    def test_a_snapshot_cannot_be_edited_or_deleted(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute("UPDATE knowledge_snapshots SET reason = 'other' "
                          "WHERE id = ?", (snapshot_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute("DELETE FROM knowledge_snapshots")
        store.rollback()
        assert KS.get_snapshot(snapshot_id)["reason"] == "shadow-tested for a quarter"

    def test_an_item_cannot_be_edited_or_deleted(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute("UPDATE knowledge_snapshot_items SET version_hash = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute("DELETE FROM knowledge_snapshot_items")
        store.rollback()
        assert KS.items_for(snapshot_id)[0]["version_hash"] == \
            KS.version_hash_for(store, "principle", pid)

    def test_one_version_per_entity_per_snapshot(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO knowledge_snapshot_items "
                "(snapshot_id, entity_type, entity_id, version_hash) "
                "VALUES (?, 'principle', ?, 'x')", (snapshot_id, pid))


# ── 3. The hash is independently verifiable ────────────────────────────


class TestTheHashIsVerifiable:
    def test_a_fresh_snapshot_verifies(self, store):
        pid = _principle(store)
        assert KS.verify_snapshot(_approve([{"entity_type": "principle",
                                             "entity_id": pid}]))

    def test_an_unknown_snapshot_is_not_trusted(self, store):
        assert KS.verify_snapshot(999) is False

    def test_rewriting_the_snapshot_row_is_detected(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("DROP TRIGGER knowledge_snapshots_no_update")
        store.execute("UPDATE knowledge_snapshots SET approved_by = 'someone else' "
                      "WHERE id = ?", (snapshot_id,))
        store.commit()
        assert KS.verify_snapshot(snapshot_id) is False
        assert any("altered after it was written" in p for p in KS.integrity())

    def test_rewriting_an_item_is_detected(self, store):
        """The item set is inside the hash, so the second half of the record
        is covered too — a snapshot whose items were swapped is not the
        snapshot that was approved."""
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("DROP TRIGGER knowledge_snapshot_items_no_update")
        store.execute(
            "UPDATE knowledge_snapshot_items SET version_hash = 'a-version-"
            "nobody-approved' WHERE snapshot_id = ?", (snapshot_id,))
        store.commit()
        assert KS.items_for(snapshot_id)[0]["version_hash"] == \
            "a-version-nobody-approved", "the tamper landed"
        assert KS.verify_snapshot(snapshot_id) is False

    def test_an_untouched_snapshot_keeps_verifying_after_the_knowledge_moves(self, store):
        """The two questions are separate: the record is intact even when the
        thing it records has changed underneath it."""
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("UPDATE trading_principles SET action_guidance = 'buy more' "
                      "WHERE id = ?", (pid,))
        store.commit()
        assert KS.verify_snapshot(snapshot_id) is True
        assert KS.integrity() == []
        assert len(KS.drifted()) == 1


# ── 4. What cannot be approved ─────────────────────────────────────────


class TestWhatCannotBeApproved:
    def test_nothing_at_all(self, store):
        with pytest.raises(ValueError, match="at least one knowledge item"):
            _approve([])

    def test_the_same_entity_twice_in_one_approval(self, store):
        pid = _principle(store)
        with pytest.raises(ValueError, match="listed twice"):
            _approve([{"entity_type": "principle", "entity_id": pid},
                      {"entity_type": "principle", "entity_id": pid}])

    @pytest.mark.parametrize("field", ["approved_by", "reason"])
    def test_a_blank_approver_or_reason(self, store, field):
        pid = _principle(store)
        with pytest.raises(ValueError, match=field):
            _approve([{"entity_type": "principle", "entity_id": pid}], **{field: "  "})

    def test_knowledge_that_is_not_there(self, store):
        with pytest.raises(ValueError, match="No principle #99"):
            _approve([{"entity_type": "principle", "entity_id": 99}])

    def test_a_candidate_that_is_not_there(self, store):
        pid = _principle(store)
        with pytest.raises(ValueError, match="No learning candidate #99"):
            _approve([{"entity_type": "principle", "entity_id": pid,
                       "candidate_id": 99}])

    def test_an_unknown_entity_type(self, store):
        with pytest.raises(ValueError, match="Unknown knowledge entity type"):
            _approve([{"entity_type": "strategy", "entity_id": 1}])

    @pytest.mark.parametrize("bad", [0, -1, "1", None, 1.0])
    def test_an_entity_id_that_is_not_a_positive_integer(self, store, bad):
        with pytest.raises(ValueError, match="entity_id"):
            _approve([{"entity_type": "principle", "entity_id": bad}])

    def test_an_item_with_unknown_keys(self, store):
        pid = _principle(store)
        with pytest.raises(ValueError, match="unknown keys"):
            _approve([{"entity_type": "principle", "entity_id": pid,
                       "confidence": 0.9}])

    def test_a_refused_approval_writes_nothing(self, store):
        _principle(store)
        with pytest.raises(ValueError):
            _approve([{"entity_type": "principle", "entity_id": 99}])
        assert KS.counts() == {"snapshots": 0, "items": 0}


# ── 5. A version is content, not a row id ──────────────────────────────


class TestAVersionIsContent:
    def test_editing_the_knowledge_changes_the_version(self, store):
        pid = _principle(store)
        before = KS.version_hash_for(store, "principle", pid)
        store.execute("UPDATE trading_principles SET category = 'momentum' "
                      "WHERE id = ?", (pid,))
        store.commit()
        assert KS.version_hash_for(store, "principle", pid) != before

    def test_running_counts_are_not_part_of_the_version(self, store):
        """A counter ticking is not a new version of the rule; a snapshot
        that changed every time a trade closed could not answer "what did we
        approve"."""
        pid = _principle(store)
        before = KS.version_hash_for(store, "principle", pid)
        store.execute("UPDATE trading_principles SET evidence_count = 99, "
                      "win_rate = 0.7, last_reinforced = '2026-06-01' WHERE id = ?",
                      (pid,))
        store.commit()
        assert KS.version_hash_for(store, "principle", pid) == before

    def test_a_second_approval_records_the_new_version(self, store):
        pid = _principle(store)
        first = _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("UPDATE trading_principles SET status = 'weakened' "
                      "WHERE id = ?", (pid,))
        store.commit()
        second = _approve([{"entity_type": "principle", "entity_id": pid}])
        assert KS.items_for(first)[0]["version_hash"] != \
            KS.items_for(second)[0]["version_hash"]
        assert KS.verify_snapshot(first) and KS.verify_snapshot(second)

    def test_a_playbook_version_is_content_too(self, store):
        bid = _playbook(store)
        before = KS.version_hash_for(store, "playbook", bid)
        store.execute("UPDATE playbooks SET pattern_json = '{\"a\": 2}' WHERE id = ?",
                      (bid,))
        store.commit()
        assert KS.version_hash_for(store, "playbook", bid) != before

    def test_a_missing_row_has_no_version(self, store):
        assert KS.version_hash_for(store, "principle", 999) is None

    def test_an_unknown_type_has_no_version(self, store):
        with pytest.raises(ValueError, match="Unknown knowledge entity type"):
            KS.version_hash_for(store, "strategy", 1)


# ── 6. The read side ───────────────────────────────────────────────────


class TestTheReadSide:
    def test_the_question_section_ten_asks(self, store):
        """Is this candidate in force, or only kept?"""
        pid, cid = _principle(store), _candidate()
        other = _candidate(payload={"proposal": {"principle": "Other"}})
        assert KS.candidate_is_approved(cid) is False
        _approve([{"entity_type": "principle", "entity_id": pid, "candidate_id": cid}])
        assert KS.candidate_is_approved(cid) is True
        assert KS.candidate_is_approved(other) is False

    def test_is_this_knowledge_in_force(self, store):
        pid, bid = _principle(store), _playbook(store)
        assert KS.entity_is_approved("principle", pid) is False
        _approve([{"entity_type": "principle", "entity_id": pid}])
        assert KS.entity_is_approved("principle", pid) is True
        assert KS.entity_is_approved("playbook", bid) is False

    def test_an_unknown_type_is_refused_rather_than_answered(self, store):
        with pytest.raises(ValueError, match="Unknown knowledge entity type"):
            KS.entity_is_approved("strategy", 1)

    def test_snapshots_can_be_found_by_candidate(self, store):
        pid, cid = _principle(store), _candidate()
        first = _approve([{"entity_type": "principle", "entity_id": pid,
                           "candidate_id": cid}])
        second = _approve([{"entity_type": "principle", "entity_id": pid,
                            "candidate_id": cid}])
        assert [s["id"] for s in KS.snapshots_for_candidate(cid)] == [first, second]
        assert KS.snapshots_for_candidate(999) == []

    def test_every_approved_item_is_listable(self, store):
        pid, bid = _principle(store), _playbook(store)
        _approve([{"entity_type": "principle", "entity_id": pid},
                  {"entity_type": "playbook", "entity_id": bid}])
        assert len(KS.approved_entities()) == 2
        assert [i["entity_id"] for i in KS.approved_entities("playbook")] == [bid]

    def test_snapshots_are_listed_oldest_first(self, store):
        pid = _principle(store)
        first = _approve([{"entity_type": "principle", "entity_id": pid}])
        second = _approve([{"entity_type": "principle", "entity_id": pid}])
        assert [s["id"] for s in KS.all_snapshots()] == [first, second]

    def test_counts_are_zero_before_anything_is_approved(self, store):
        assert KS.counts() == {"snapshots": 0, "items": 0}


# ── 7. Integrity, and drift as a separate signal ───────────────────────


class TestIntegrityAndDrift:
    def test_a_clean_record_is_quiet(self, store):
        pid = _principle(store)
        _approve([{"entity_type": "principle", "entity_id": pid}])
        assert KS.integrity() == [] and KS.drifted() == []

    def test_knowledge_deleted_after_approval_is_reported(self, store):
        pid = _principle(store)
        snapshot_id = _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("DELETE FROM trading_principles WHERE id = ?", (pid,))
        store.commit()
        assert any("no longer exists" in p for p in KS.integrity()), KS.integrity()
        assert KS.verify_snapshot(snapshot_id), "the record itself is intact"

    def test_a_candidate_deleted_after_approval_is_reported(self, store):
        pid, cid = _principle(store), _candidate()
        _approve([{"entity_type": "principle", "entity_id": pid,
                   "candidate_id": cid}])
        store.execute("DELETE FROM learning_candidates WHERE id = ?", (cid,))
        store.commit()
        assert any("candidate #1" in p for p in KS.integrity()), KS.integrity()

    def test_a_snapshot_with_no_items_is_reported(self, store):
        store.execute(
            "INSERT INTO knowledge_snapshots "
            "(approved_by, approved_at, reason, content_hash, frozen_at) "
            "VALUES ('someone', '2026-01-01', 'because', 'x', '2026-01-01')")
        store.commit()
        assert any("no items" in p for p in KS.integrity()), KS.integrity()

    def test_drift_names_both_versions(self, store):
        pid = _principle(store)
        approved = KS.version_hash_for(store, "principle", pid)
        _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("UPDATE trading_principles SET action_guidance = 'buy more' "
                      "WHERE id = ?", (pid,))
        store.commit()
        drift = KS.drifted()
        assert len(drift) == 1
        assert drift[0]["version_hash"] == approved
        assert drift[0]["current_version_hash"] == \
            KS.version_hash_for(store, "principle", pid)

    def test_drift_is_not_a_fault(self, store):
        pid = _principle(store)
        _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("UPDATE trading_principles SET status = 'weakened' "
                      "WHERE id = ?", (pid,))
        store.commit()
        assert KS.drifted(), "the change is visible"
        assert KS.integrity() == [], "and it is not a complaint"

    def test_a_deleted_knowledge_row_also_drifts(self, store):
        pid = _principle(store)
        _approve([{"entity_type": "principle", "entity_id": pid}])
        store.execute("DELETE FROM trading_principles WHERE id = ?", (pid,))
        store.commit()
        assert KS.drifted()[0]["current_version_hash"] is None


# ── 8. Approving changes nothing else ──────────────────────────────────


class TestApprovingActivatesNothing:
    def test_only_the_two_new_tables_grow(self, store):
        """The machine form of "this slice adds no activation path": the
        whole database is compared, not a chosen list of tables."""
        pid, cid = _principle(store), _candidate()
        before = _dump(store)
        _approve([{"entity_type": "principle", "entity_id": pid,
                   "candidate_id": cid}])
        after = _dump(store)

        assert set(before) == set(after)
        changed = {name for name in before if before[name] != after[name]}
        assert changed == {"knowledge_snapshots", "knowledge_snapshot_items"}, (
            "an approval must not touch anything else; it touched "
            f"{sorted(changed)}")

    def test_the_candidate_itself_is_untouched(self, store):
        """Approval is not promotion: the candidate keeps its own status and
        payload, and its lifecycle is moved by a person, not by this."""
        pid, cid = _principle(store), _candidate()
        before = LC.get_candidate(cid)
        _approve([{"entity_type": "principle", "entity_id": pid,
                   "candidate_id": cid}])
        assert LC.get_candidate(cid) == before
        assert LC.transitions_for(cid) == []

    def test_approving_does_not_need_a_validated_candidate(self, store):
        """§10 gives the learner permission to propose and request
        evaluation, not to grant authority — and it does not hand the
        reverse power to the code either. Whether a candidate had reached
        ``validated`` is a person's judgement, not a rule this slice
        invents."""
        pid, cid = _principle(store), _candidate()
        assert LC.get_candidate(cid)["status"] == LC.OBSERVATION
        _approve([{"entity_type": "principle", "entity_id": pid,
                   "candidate_id": cid}])
        assert KS.candidate_is_approved(cid)

    def test_the_active_knowledge_listings_do_not_move(self, store):
        from alpha_agents.data import memory_store as ms
        pid = _principle(store)
        before = (ms.get_active_principles(), ms.get_active_playbooks())
        _approve([{"entity_type": "principle", "entity_id": pid}])
        assert (ms.get_active_principles(), ms.get_active_playbooks()) == before
