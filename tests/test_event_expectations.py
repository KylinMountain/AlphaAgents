"""Event expectations are point-in-time snapshots, never mutable state."""

import sqlite3

import pytest

from alpha_agents.data import event_expectations as E


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    E.init_schema(db)
    return db


def _seed(conn):
    E.record_event(
        event_key="fed-2026-09", event_type="macro", scope="global",
        subject="FOMC", scheduled_at="2026-09-20 02:00:00",
        captured_at="2026-09-01 08:00:00", source="fed",
        metadata={"decision": "policy_rate"}, conn=conn)
    E.record_expectation(
        event_key="fed-2026-09", captured_at="2026-09-10 12:00:00",
        source="futures", market_implied={"cut_25bp_prob": 0.60}, conn=conn)
    E.record_expectation(
        event_key="fed-2026-09", captured_at="2026-09-19 12:00:00",
        source="futures", market_implied={"cut_25bp_prob": 0.92}, conn=conn)
    E.record_realization(
        event_key="fed-2026-09", announced_at="2026-09-20 02:00:00",
        actual={"decision_bp": -25}, source="fed", conn=conn)


def test_expectation_is_as_of_not_latest(conn):
    _seed(conn)
    rows = E.context(
        as_of="2026-09-15 09:00:00", subject="FOMC", conn=conn)
    assert rows[0]["expectation"]["market_implied"]["cut_25bp_prob"] == 0.60


def test_realization_does_not_leak_before_announcement(conn):
    _seed(conn)
    before = E.context(
        as_of="2026-09-19 09:00:00", subject="FOMC", conn=conn)
    after = E.context(
        as_of="2026-09-20 03:00:00", subject="FOMC", conn=conn)
    assert before[0]["realization"] is None
    assert after[0]["realization"]["actual"]["decision_bp"] == -25


def test_revised_expectation_is_visible_only_after_capture(conn):
    _seed(conn)
    rows = E.context(
        as_of="2026-09-19 13:00:00", subject="FOMC", conn=conn)
    assert rows[0]["expectation"]["market_implied"]["cut_25bp_prob"] == 0.92


def test_snapshots_are_append_only(conn):
    _seed(conn)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "UPDATE event_expectation_snapshots "
            "SET source='rewritten' WHERE event_key='fed-2026-09'")
