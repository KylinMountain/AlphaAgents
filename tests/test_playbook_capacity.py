"""G4 — the playbook set stays bounded, and the weakest are named first.

Phase one turned the cap from an enforcement rule into a measurement: the
weakest rules are written to the candidate store, and no active rule moves
until a candidate is approved. These tests pin the ordering and the
non-mutation, which is what the original rule was really protecting.
"""

import json
import sqlite3

import pytest

from alpha_agents.evolution import playbook as pbmod
from alpha_agents.evolution.playbook import (
    MAX_ACTIVE_PLAYBOOKS, _playbook_utility, enforce_capacity,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store as ms

    conn = sqlite3.connect(str(tmp_path / "memory.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(ms._SCHEMA)
    conn.commit()
    monkeypatch.setattr(ms, "_get_conn", lambda: conn)
    yield ms, conn
    conn.close()


def _add(conn, name, *, hit_rate, trades, status="active"):
    conn.execute(
        "INSERT INTO playbooks (name, pattern_json, created_date, last_updated, "
        "status, weight, total_trades, wins, hit_rate) "
        "VALUES (?, ?, '2026-09-01', '2026-09-01', ?, 1.0, ?, ?, ?)",
        (name, json.dumps({"conditions": [{"field": "theme", "op": "==",
                                           "value": name}]}),
         status, trades, int(trades * hit_rate), hit_rate),
    )
    conn.commit()


class TestUtility:
    def test_untested_sits_at_the_base_rate(self):
        """A brand new playbook must not be evicted before measurement."""
        assert _playbook_utility({"total_trades": 0, "hit_rate": 0.0}) == 0.5

    def test_good_beats_bad(self):
        good = _playbook_utility({"total_trades": 20, "hit_rate": 0.7})
        bad = _playbook_utility({"total_trades": 20, "hit_rate": 0.2})
        assert good > 0.5 > bad

    def test_thin_evidence_shrinks_toward_the_base_rate(self):
        thin = _playbook_utility({"total_trades": 3, "hit_rate": 0.9})
        thick = _playbook_utility({"total_trades": 20, "hit_rate": 0.9})
        assert 0.5 < thin < thick

    def test_missing_fields_do_not_raise(self):
        assert _playbook_utility({}) == 0.5


class TestEnforceCapacity:
    """Over-capacity is measured, not enforced.

    Retirement changes what retrieval ranks, so it needs a candidate version.
    The cap is a governance target: the excess is queued and the active set
    survives intact until something is approved.
    """

    @staticmethod
    def _candidates(conn):
        return conn.execute(
            "SELECT id, entity_type, operation, target_id, payload_json "
            "FROM learning_candidates ORDER BY id"
        ).fetchall()

    @staticmethod
    def _active_names(conn):
        return {r["name"] for r in conn.execute(
            "SELECT name FROM playbooks WHERE status = 'active'")}

    def test_under_the_cap_is_a_no_op(self, store):
        _ms, conn = store
        for i in range(3):
            _add(conn, f"pb{i}", hit_rate=0.6, trades=10)
        assert enforce_capacity("2026-09-07") == []

    def test_exactly_at_the_cap_is_a_no_op(self, store):
        _ms, conn = store
        for i in range(MAX_ACTIVE_PLAYBOOKS):
            _add(conn, f"pb{i}", hit_rate=0.6, trades=10)
        assert enforce_capacity("2026-09-07") == []

    def test_over_the_cap_quarantines_the_weakest(self, store):
        _ms, conn = store
        # One clear loser among otherwise healthy playbooks.
        _add(conn, "loser", hit_rate=0.1, trades=20)
        for i in range(MAX_ACTIVE_PLAYBOOKS):
            _add(conn, f"good{i}", hit_rate=0.7, trades=20)

        returned = enforce_capacity("2026-09-07")
        assert len(returned) == 1

        # The returned id is a candidate, not a retired playbook.
        candidates = self._candidates(conn)
        assert [c["id"] for c in candidates] == returned
        assert candidates[0]["operation"] == "retire"
        assert candidates[0]["entity_type"] == "playbook"

        target = conn.execute(
            "SELECT name FROM playbooks WHERE id = ?", (candidates[0]["target_id"],)
        ).fetchone()
        assert target["name"] == "loser"

        # And the live row has not moved.
        row = conn.execute(
            "SELECT status FROM playbooks WHERE name = 'loser'"
        ).fetchone()
        assert row["status"] == "active"

    def test_quarantines_down_to_the_cap_exactly(self, store):
        _ms, conn = store
        for i in range(MAX_ACTIVE_PLAYBOOKS + 5):
            _add(conn, f"pb{i}", hit_rate=i / 100.0, trades=20)

        returned = enforce_capacity("2026-09-07")
        assert len(returned) == 5
        # Nothing was evicted; the active set is still over the cap.
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM playbooks WHERE status = 'active'"
        ).fetchone()["c"]
        assert remaining == MAX_ACTIVE_PLAYBOOKS + 5

    def test_the_queued_candidates_are_the_weakest(self, store):
        _ms, conn = store
        for i in range(MAX_ACTIVE_PLAYBOOKS + 3):
            _add(conn, f"pb{i}", hit_rate=i / 20.0, trades=20)

        enforce_capacity("2026-09-07")
        targets = {r["name"] for r in conn.execute(
            "SELECT p.name FROM learning_candidates c "
            "JOIN playbooks p ON p.id = c.target_id")}
        # The three lowest hit rates are the ones that should have been named,
        # and naming them changed nothing about what is still live.
        assert targets == {"pb0", "pb1", "pb2"}
        assert {"pb0", "pb1", "pb2"} <= self._active_names(conn)
        assert len(self._active_names(conn)) == MAX_ACTIVE_PLAYBOOKS + 3

    def test_deprecated_ones_are_not_counted_against_the_cap(self, store):
        _ms, conn = store
        for i in range(MAX_ACTIVE_PLAYBOOKS):
            _add(conn, f"active{i}", hit_rate=0.6, trades=10)
        for i in range(10):
            _add(conn, f"old{i}", hit_rate=0.1, trades=20, status="deprecated")
        assert enforce_capacity("2026-09-07") == []

    def test_the_row_is_kept_for_audit(self, store):
        """Nothing is deprecated and nothing is deleted.

        The retiring rule keeps its status and history; the reasoning that
        nominated it lives in the candidate payload instead.
        """
        _ms, conn = store
        _add(conn, "loser", hit_rate=0.05, trades=30)
        for i in range(MAX_ACTIVE_PLAYBOOKS):
            _add(conn, f"good{i}", hit_rate=0.7, trades=20)

        enforce_capacity("2026-09-07")
        row = conn.execute(
            "SELECT status, version_history FROM playbooks WHERE name = 'loser'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert "容量上限" not in (row["version_history"] or "")

        payload = json.loads(self._candidates(conn)[0]["payload_json"])
        assert payload["capacity"] == MAX_ACTIVE_PLAYBOOKS
        assert payload["playbook"]["name"] == "loser"
        assert payload["proposal"]["status"] == "deprecated"

    def test_no_playbooks_is_a_no_op(self, store):
        assert enforce_capacity("2026-09-07") == []


class TestAutoCreateDoesNotTouchTheActiveSet:
    def test_novel_clusters_are_quarantined_not_created(self, store, monkeypatch):
        _ms, conn = store
        for i in range(MAX_ACTIVE_PLAYBOOKS):
            _add(conn, f"good{i}", hit_rate=0.7, trades=20)

        # Plenty of novel clusters on offer. Discovery no longer needs to
        # free a slot first, because it cannot enlarge the active set at all.
        monkeypatch.setattr(pbmod, "_query_hit_clusters", lambda **kw: [
            {"theme": f"新主题{i}", "hits": 5, "total": 6,
             "institutional_present": 0}
            for i in range(5)
        ])
        created = pbmod.scan_and_auto_create("2026-09-07")
        assert len(created) == 5

        active = conn.execute(
            "SELECT COUNT(*) c FROM playbooks WHERE status = 'active'"
        ).fetchone()["c"]
        assert active == MAX_ACTIVE_PLAYBOOKS

        rows = conn.execute(
            "SELECT entity_type, operation FROM learning_candidates"
        ).fetchall()
        assert len(rows) == 5
        assert {r["entity_type"] for r in rows} == {"playbook"}
        assert {r["operation"] for r in rows} == {"create"}
