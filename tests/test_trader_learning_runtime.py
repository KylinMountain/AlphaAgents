"""T7: repeated evidence, not one review, creates Trader Lessons/Rules."""
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


def candidate(conn, index: int, *, confidence=.7, counter=0,
              timeframe="1d", scope="replay_daily",
              claim="强主题突破时不要只等深回踩"):
    return D.save_lesson_candidate(
        run_id="run-a", trader_id="default",
        source_type="decision_review",
        source_ref=f"decision:{index}",
        source_date=f"2026-09-{10 + index:02d}",
        claim=claim,
        action="adjust",
        applicable_context="强主题突破",
        evidence_timeframe=timeframe,
        decision_horizon="3-5d",
        evidence_scope=scope,
        support_count=999,  # must NOT let one model response self-promote
        counterexample_count=counter,
        confidence=confidence,
        evidence={"decision": index},
        conn=conn)


def test_one_candidate_cannot_self_promote_even_if_it_claims_999_support(conn):
    candidate(conn, 1)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert got["lessons_created"] == 0
    assert got["rules_created"] == 0
    assert D.lessons(run_id="run-a", trader_id="default", conn=conn) == []
    assert D.rules(run_id="run-a", trader_id="default", conn=conn) == []


def test_three_independent_experiences_create_lesson_not_rule(conn):
    for i in range(1, 4):
        candidate(conn, i)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert got["lessons_created"] == 1
    assert got["rules_created"] == 0
    [lesson] = D.lessons(run_id="run-a", trader_id="default", conn=conn)
    assert lesson["support_count"] == 3
    assert lesson["counterexample_count"] == 0


def test_five_independent_experiences_create_expiring_active_rule(conn):
    for i in range(1, 6):
        candidate(conn, i)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert got["rules_created"] == 1
    [rule] = D.rules(run_id="run-a", trader_id="default", conn=conn)
    assert rule["support_count"] == 5
    assert rule["expires_on"] == "2026-12-19"
    assert D.rule_events(rule["id"], conn=conn)[-1]["event"] == "activate"


def test_counterevidence_blocks_rule_but_not_lesson(conn):
    for i in range(1, 6):
        candidate(conn, i, counter=1 if i <= 3 else 0)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert got["lessons_created"] == 1
    assert got["rules_created"] == 0


def test_low_confidence_blocks_rule(conn):
    for i in range(1, 6):
        candidate(conn, i, confidence=.4)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert got["lessons_created"] == 1
    assert got["rules_created"] == 0


def test_same_evidence_is_idempotent_and_new_evidence_versions(conn):
    for i in range(1, 6):
        candidate(conn, i)
    first = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    second = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert first["lessons_created"] == first["rules_created"] == 1
    assert second["lessons_created"] == second["rules_created"] == 0

    candidate(conn, 6)
    third = L.advance(
        trader_id="default", as_of="2026-09-21",
        run_id="run-a", conn=conn)
    assert third["lessons_created"] == third["rules_created"] == 1
    lessons = D.lessons(run_id="run-a", trader_id="default", conn=conn)
    rules = D.rules(run_id="run-a", trader_id="default", conn=conn)
    assert [row["version"] for row in lessons] == [1, 2]
    assert [row["version"] for row in rules] == [1, 2]
    assert lessons[1]["supersedes_id"] == lessons[0]["id"]
    assert rules[1]["supersedes_id"] == rules[0]["id"]


def test_daily_replay_evidence_stays_labelled_daily_when_read_intraday(conn):
    for i in range(1, 6):
        candidate(conn, i, timeframe="1d", scope="replay_daily")
    L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    text = L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-09-21", run_id="run-a", conn=conn)
    assert "RULE" in text
    assert "[1d / replay_daily" in text
    assert "5m" not in text
    assert "live_intraday" not in text


def test_rule_retirement_is_reversible_and_append_only(conn):
    for i in range(1, 6):
        candidate(conn, i)
    got = L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    rule_id = got["rule_ids"][0]

    assert "RULE" in L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-09-21", run_id="run-a", conn=conn)
    L.retire_rule(
        rule_id, reason="new counterevidence", at="2026-09-22", conn=conn)
    assert "RULE" not in L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-09-23", run_id="run-a", conn=conn)
    L.reinstate_rule(
        rule_id, reason="counterevidence resolved", at="2026-09-24", conn=conn)
    assert "RULE" in L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-09-25", run_id="run-a", conn=conn)
    assert [row["event"] for row in D.rule_events(rule_id, conn=conn)] == [
        "activate", "retire", "reinstate"]


def test_expired_rule_is_not_injected(conn):
    for i in range(1, 6):
        candidate(conn, i)
    L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert "RULE" not in L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-12-20", run_id="run-a", conn=conn)


def test_runs_do_not_share_lessons(conn):
    for i in range(1, 6):
        candidate(conn, i)
    L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    assert L.inject(
        trader_id="default", decision_horizon="3-5d",
        as_of="2026-09-21", run_id="run-b", conn=conn) == ""


def test_version_tables_are_append_only(conn):
    for i in range(1, 4):
        candidate(conn, i)
    L.advance(
        trader_id="default", as_of="2026-09-20",
        run_id="run-a", conn=conn)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE trader_lessons SET claim='edited'")
    conn.rollback()
