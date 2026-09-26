"""T7: market-graded decisions, not a model's words or counts, create Lessons/Rules."""
from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import trader_learning as D
from alpha_agents.evolution import trader_learning as L


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    db = memory_store._get_conn()
    yield db
    db.close()
    memory_store._local.conn = None


def graded(conn, index: int, *, verdict="wrong", action="buy",
           tags=("t1_up_big",), timeframe="1d", scope="replay_daily",
           horizon="3-5d", run="run-a", end="2026-09-10"):
    excess = {"right": 3.0, "wrong": -3.0, "flat": 0.1}[verdict]
    return D.save_decision_outcome(
        run_id=run, trader_id="default", decision_id=f"d{index}",
        action=action, code="600001", decided_on="2026-09-01",
        base_date="2026-09-01", end_date=end, horizon=5,
        forward_pct=excess, market_median_pct=0.0, excess_pct=excess,
        verdict=verdict, tags=list(tags), evidence_timeframe=timeframe,
        decision_horizon=horizon, evidence_scope=scope, conn=conn)


def advance(conn, as_of="2026-09-20", run="run-a"):
    return L.advance(trader_id="default", as_of=as_of, run_id=run, conn=conn)


def test_a_model_claiming_999_support_creates_nothing(conn):
    """Candidates are words; their counts are not evidence."""
    for i in range(20):
        D.save_lesson_candidate(
            run_id="run-a", trader_id="default",
            source_type="decision_review", source_ref=f"decision:{i}",
            source_date="2026-09-10", claim="强主题突破时不要只等深回踩",
            action="adjust", applicable_context="强主题突破",
            evidence_timeframe="1d", decision_horizon="3-5d",
            evidence_scope="replay_daily", support_count=999,
            counterexample_count=0, confidence=1.0, conn=conn)
    got = advance(conn)
    assert got["lessons_created"] == 0 and got["rules_created"] == 0


def test_below_the_lesson_floor_nothing_is_created(conn):
    for i in range(L.LESSON_MIN_N - 1):
        graded(conn, i)
    assert advance(conn)["lessons_created"] == 0


def test_a_mostly_wrong_cell_becomes_a_lesson_saying_so(conn):
    for i in range(8):
        graded(conn, i, verdict="wrong")
    for i in range(8, 10):
        graded(conn, i, verdict="right")
    got = advance(conn)
    assert got["lessons_created"] == 1 and got["rules_created"] == 0
    lesson = D.lessons(run_id="run-a", trader_id="default", conn=conn)[0]
    assert lesson["support_count"] == 8 and lesson["counterexample_count"] == 2
    assert lesson["confidence"] == pytest.approx(0.8)
    assert "判错 8/10" in lesson["claim"] and "多数时候是错的" in lesson["claim"]
    assert lesson["applicable_context"] == "t1_up_big"


def test_a_coin_flip_cell_teaches_nothing(conn):
    for i in range(12):
        graded(conn, i, verdict="right" if i % 2 else "wrong")
    assert advance(conn)["lessons_created"] == 0


def test_flat_verdicts_are_not_evidence(conn):
    for i in range(30):
        graded(conn, i, verdict="flat")
    assert advance(conn)["lessons_created"] == 0


def test_a_rule_needs_fifty_graded_decisions(conn):
    for i in range(L.RULE_MIN_N - 1):
        graded(conn, i, verdict="right")
    assert advance(conn)["rules_created"] == 0
    graded(conn, 999, verdict="right")
    got = advance(conn, as_of="2026-09-21")
    assert got["rules_created"] == 1
    rule = D.rules(run_id="run-a", trader_id="default", conn=conn)[0]
    assert rule["expires_on"] == "2026-12-20"
    assert [e["event"] for e in D.rule_events(rule["id"], conn=conn)] == [
        "activate"]


def test_cells_are_split_by_action_and_tag(conn):
    for i in range(10):
        graded(conn, i, action="buy", tags=("t1_up_big",))
    for i in range(10, 20):
        graded(conn, i, action="wait", tags=("t1_up_big",))
    for i in range(20, 30):
        graded(conn, i, action="buy", tags=("below_ma5",))
    assert advance(conn)["lessons_created"] == 3


def test_ungraded_windows_are_not_read(conn):
    """A window that closes after ``as_of`` is not evidence on ``as_of``."""
    for i in range(10):
        graded(conn, i, end="2026-09-25")
    assert advance(conn, as_of="2026-09-20")["lessons_created"] == 0


def test_same_evidence_is_idempotent_and_new_evidence_versions(conn):
    for i in range(10):
        graded(conn, i)
    assert advance(conn)["lessons_created"] == 1
    assert advance(conn, as_of="2026-09-21")["lessons_created"] == 0
    graded(conn, 50)
    assert advance(conn, as_of="2026-09-22")["lessons_created"] == 1
    versions = D.lessons(run_id="run-a", trader_id="default", conn=conn)
    assert [row["version"] for row in versions] == [1, 2]


def _make_rule(conn):
    for i in range(L.RULE_MIN_N):
        graded(conn, i, verdict="wrong")
    return advance(conn)["rule_ids"][0]


def test_daily_replay_evidence_stays_labelled_daily(conn):
    _make_rule(conn)
    text = L.inject(trader_id="default", decision_horizon="3-5d",
                    as_of="2026-09-21", run_id="run-a", conn=conn)
    assert "RULE" in text and "[1d / replay_daily" in text


def test_rule_retirement_is_reversible_and_append_only(conn):
    rule_id = _make_rule(conn)
    L.retire_rule(rule_id, reason="counterevidence", at="2026-09-22",
                  conn=conn)
    assert "RULE" not in L.inject(
        trader_id="default", decision_horizon="3-5d", as_of="2026-09-23",
        run_id="run-a", conn=conn)
    L.reinstate_rule(rule_id, reason="resolved", at="2026-09-24", conn=conn)
    assert "RULE" in L.inject(
        trader_id="default", decision_horizon="3-5d", as_of="2026-09-25",
        run_id="run-a", conn=conn)


def test_expired_rule_is_not_injected(conn):
    _make_rule(conn)
    assert "RULE" not in L.inject(
        trader_id="default", decision_horizon="3-5d", as_of="2026-12-30",
        run_id="run-a", conn=conn)


def test_runs_do_not_share_lessons(conn):
    _make_rule(conn)
    assert L.inject(trader_id="default", decision_horizon="3-5d",
                    as_of="2026-09-21", run_id="run-b", conn=conn) == ""


def test_same_evidence_next_day_does_not_extend_rule_expiry(conn):
    rule_id = _make_rule(conn)
    assert advance(conn, as_of="2026-09-21")["rules_created"] == 0
    rows = D.rules(run_id="run-a", trader_id="default", conn=conn)
    assert [row["id"] for row in rows] == [rule_id]
    assert rows[0]["expires_on"] == "2026-12-19"


def test_version_and_outcome_tables_are_append_only(conn):
    for i in range(10):
        graded(conn, i)
    advance(conn)
    for table in ("trader_lessons", "trader_decision_outcomes"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(f"UPDATE {table} SET run_id='edited'")
        conn.rollback()
