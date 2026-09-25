"""Persistent TraderState contracts for T3.

The store is append-only, scoped by run/trader identity and uses compare-and-swap
on the parent state hash. A replay branch may start from the same cognitive
snapshot as live without sharing persistence identity.
"""
from datetime import datetime, timedelta
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.data.memory_schema import _SCHEMA
from alpha_agents.data import trader_state_store as S
from alpha_agents.trader import TraderRuntimeError, TraderState

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.commit()
    yield db
    db.close()


def state(*, trader="default", minute=0):
    return TraderState.create(
        trader_id=trader,
        as_of=datetime(2026, 9, 25, 9, minute, tzinfo=TZ),
        market_view={"regime": "range"},
    )


def advance(value, *, minute):
    return value.evolve(
        as_of=datetime(2026, 9, 25, 9, minute, tzinfo=TZ),
        market_view={"regime": "range", "minute": minute},
    )


def test_initial_state_round_trip(conn):
    original = state()
    assert S.save(original, run_id="live", conn=conn)
    restored = S.load_latest(run_id="live", trader_id="default", conn=conn)
    assert restored == original
    assert restored.state_hash == original.state_hash


def test_exact_retry_is_idempotent(conn):
    original = state()
    assert S.save(original, run_id="live", conn=conn)
    assert not S.save(original, run_id="live", conn=conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM trader_state_snapshots"
    ).fetchone()[0]
    assert count == 1


def test_next_state_requires_exact_parent(conn):
    original = state()
    S.save(original, run_id="live", conn=conn)
    next_state = advance(original, minute=5)
    assert S.save(next_state, run_id="live", conn=conn)
    assert S.load_latest(
        run_id="live", trader_id="default", conn=conn
    ).state_hash == next_state.state_hash


def test_stale_writer_cannot_overwrite_newer_state(conn):
    original = state()
    S.save(original, run_id="live", conn=conn)
    winner = advance(original, minute=5)
    loser = advance(original, minute=6)
    S.save(winner, run_id="live", conn=conn)
    with pytest.raises(S.TraderStateConflict, match="parent hash"):
        S.save(loser, run_id="live", conn=conn)
    assert S.load_latest(
        run_id="live", trader_id="default", conn=conn
    ).state_hash == winner.state_hash


def test_same_state_hash_is_valid_in_independent_runs(conn):
    original = state()
    assert S.save(original, run_id="live", conn=conn)
    assert S.save(original, run_id="replay-001", conn=conn)
    rows = conn.execute(
        "SELECT run_id,state_hash FROM trader_state_snapshots ORDER BY run_id"
    ).fetchall()
    assert [(row["run_id"], row["state_hash"]) for row in rows] == [
        ("live", original.state_hash),
        ("replay-001", original.state_hash),
    ]


def test_runs_advance_independently_from_same_initial_snapshot(conn):
    original = state()
    S.save(original, run_id="branch-a", conn=conn)
    S.save(original, run_id="branch-b", conn=conn)
    a = advance(original, minute=5)
    b = original.evolve(
        as_of=datetime(2026, 9, 25, 9, 7, tzinfo=TZ),
        market_view={"regime": "trend"},
    )
    S.save(a, run_id="branch-a", conn=conn)
    S.save(b, run_id="branch-b", conn=conn)
    assert S.load_latest(
        run_id="branch-a", trader_id="default", conn=conn
    ).state_hash == a.state_hash
    assert S.load_latest(
        run_id="branch-b", trader_id="default", conn=conn
    ).state_hash == b.state_hash


def test_traders_are_isolated_inside_one_run(conn):
    a = state(trader="a")
    b = state(trader="b")
    S.save(a, run_id="live", conn=conn)
    S.save(b, run_id="live", conn=conn)
    assert S.load_latest(
        run_id="live", trader_id="a", conn=conn
    ).trader_id == "a"
    assert S.load_latest(
        run_id="live", trader_id="b", conn=conn
    ).trader_id == "b"


def test_store_is_append_only(conn):
    original = state()
    S.save(original, run_id="live", conn=conn)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "UPDATE trader_state_snapshots SET as_of='x' "
            "WHERE run_id='live'"
        )
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM trader_state_snapshots WHERE run_id='live'")


def test_load_detects_payload_tampering_even_if_inserted_out_of_band(conn):
    original = state()
    payload = original.as_dict()
    payload["market_view"]["regime"] = "tampered"
    conn.execute(
        "INSERT INTO trader_state_snapshots "
        "(state_hash,run_id,trader_id,version,parent_state_hash,as_of,payload_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            original.state_hash, "bad", "default", 0, None,
            original.as_of.isoformat(),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()
    with pytest.raises(TraderRuntimeError, match="hash mismatch"):
        S.load_latest(run_id="bad", trader_id="default", conn=conn)


def test_empty_identity_is_refused(conn):
    with pytest.raises(S.TraderStateStoreError, match="run_id"):
        S.save(state(), run_id="", conn=conn)
    with pytest.raises(S.TraderStateStoreError, match="trader_id"):
        S.load_latest(run_id="live", trader_id="", conn=conn)


def test_store_refuses_ambient_transaction(conn):
    original = state()
    conn.execute("CREATE TABLE unrelated(x INTEGER)")
    conn.execute("INSERT INTO unrelated VALUES (1)")
    assert conn.in_transaction
    with pytest.raises(S.TraderStateStoreError, match="clean transaction"):
        S.save(original, run_id="live", conn=conn)
