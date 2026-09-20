"""RP-09: forward evidence is isolated, append-only and time-honest."""

from datetime import datetime
import sqlite3

import pytest

from alpha_agents.evolution import forward_observation as F


@pytest.fixture()
def conn(tmp_path):
    c = F.connect(tmp_path / "forward-observation.db")
    yield c
    c.close()


def test_identical_capture_retry_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(
        F, "_utc_now", lambda: "2026-09-20T04:00:00.000000+00:00")
    kwargs = dict(
        source="exchange",
        source_key="announcement:1",
        status="ok",
        collector_version="collector-v1",
        payload={"value": 1},
        source_timezone="Asia/Shanghai",
        source_published_at="2026-09-20T11:50:00+08:00",
        source_captured_at="2026-09-20T11:59:50+08:00",
        available_at="2026-09-20T11:50:00+08:00",
    )
    first = F.record_capture(conn, **kwargs)
    second = F.record_capture(conn, **kwargs)
    assert first == second
    assert conn.execute(
        "SELECT COUNT(*) FROM forward_captures").fetchone()[0] == 1
    row = F.capture(conn, first)
    assert row["registered_at"] == "2026-09-20T04:00:00.000000+00:00"
    assert row["evidence_scope"] == F.OBSERVATION_ONLY


def test_source_revision_appends_and_preserves_old_version(conn, monkeypatch):
    times = iter([
        "2026-09-20T04:00:00.000000+00:00",
        "2026-09-20T04:01:00.000000+00:00",
    ])
    monkeypatch.setattr(F, "_utc_now", lambda: next(times))

    first = F.record_capture(
        conn, source="provider", source_key="quote:600000:20260920",
        status="ok", collector_version="v1", payload={"price": 10.0})
    second = F.record_capture(
        conn, source="provider", source_key="quote:600000:20260920",
        status="ok", collector_version="v1", payload={"price": 10.1})

    assert second != first
    old = F.capture(conn, first)
    new = F.capture(conn, second)
    assert old["payload_json"] != new["payload_json"]
    assert new["supersedes_id"] == first
    assert old["supersedes_id"] is None


@pytest.mark.parametrize(
    "status",
    ["empty", "unknown", "failure", "permission_denied", "timeout"],
)
def test_non_success_capture_states_are_records_not_absences(conn, status):
    row_id = F.record_capture(
        conn, source="provider", source_key=f"k:{status}",
        status=status, collector_version="v1",
        payload=None, error=f"{status}: detail")
    row = F.capture(conn, row_id)
    assert row["status"] == status
    assert row["error"] == f"{status}: detail"


def test_registered_at_cannot_be_backfilled_by_caller(conn):
    with pytest.raises(TypeError):
        F.record_capture(
            conn, source="provider", source_key="k",
            status="ok", collector_version="v1",
            registered_at="2020-01-01T00:00:00Z")  # type: ignore[call-arg]


def test_decision_retry_is_idempotent_and_revision_appends(conn, monkeypatch):
    times = iter([
        "2026-09-20T04:00:00.000000+00:00",
        "2026-09-20T04:02:00.000000+00:00",
    ])
    monkeypatch.setattr(F, "_utc_now", lambda: next(times))
    base = dict(
        decision_key="2026-09-20:09:00:sector",
        status="abstained",
        information_cutoff="2026-09-20T09:00:00+08:00",
        code_ref="a" * 40,
        policy_ref="policy#1@abc",
        world_read_set_hash="w" * 64,
        research_packet_hash="p" * 64,
        model_config={"model": "x", "temperature": 0},
        request={"panel": ["600000"]},
        budget={"calls": 0},
        result={"reason": "no trade"},
    )
    first = F.record_decision(conn, **base)
    assert F.record_decision(conn, **base) == first

    changed = dict(base)
    changed["status"] = "selected"
    changed["result"] = {"code": "600000"}
    second = F.record_decision(conn, **changed)

    assert second != first
    assert F.decision(conn, second)["supersedes_id"] == first
    assert F.decision(conn, first)["status"] == "abstained"
    assert F.decision(conn, second)["evidence_scope"] == F.OBSERVATION_ONLY


@pytest.mark.parametrize(
    "status",
    [
        "no_candidate", "parse_error", "technical_failure",
        "permission_denied", "timeout",
    ],
)
def test_decision_failure_and_no_action_states_are_preserved(conn, status):
    row_id = F.record_decision(
        conn,
        decision_key=f"d:{status}",
        status=status,
        information_cutoff="2026-09-20T09:00:00+08:00",
        code_ref="a" * 40,
        policy_ref="policy#1",
        world_read_set_hash="w" * 64,
        model_config={"model": "x"},
        request={},
        budget={"used": 0},
        result=None,
        error=f"{status}: detail",
    )
    row = F.decision(conn, row_id)
    assert row["status"] == status
    assert row["error"] == f"{status}: detail"


def test_tables_are_append_only(conn):
    row_id = F.record_capture(
        conn, source="provider", source_key="k",
        status="ok", collector_version="v1", payload={"x": 1})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "UPDATE forward_captures SET status='failure' WHERE id=?",
            (row_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM forward_captures WHERE id=?", (row_id,))


def test_store_has_no_policy_or_trading_mutation_surface():
    forbidden = {
        "promote", "create_pending_order", "open_position",
        "set_active_policy", "activate",
    }
    assert forbidden.isdisjoint(set(dir(F)))


def test_registered_at_is_offset_aware_utc(conn):
    row_id = F.record_capture(
        conn, source="provider", source_key="time",
        status="ok", collector_version="v1", payload={})
    parsed = datetime.fromisoformat(F.capture(conn, row_id)["registered_at"])
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0
