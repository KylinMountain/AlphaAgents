"""Pure frame identities, append-only capture and read-only exports."""
import asyncio
from dataclasses import FrozenInstanceError
import json
import sqlite3

import pytest

from alpha_agents.data.decision_frame import DecisionFrame, FrameError, TraderState
from alpha_agents.data.decision_capture import Capture, read_inputs
from alpha_agents.data import memory_store
from alpha_agents.evolution.replay_mode import replay_as_of


def frame(*, trader="a", run="r1", tools=None, knowledge="R2: only leaders", origin="test"):
    return DecisionFrame.create(
        identity={"run_id": run, "trader_id": trader, "stage": "trade_plan", "phase": "open",
                  "session_day": "2026-01-19", "information_cutoff": "2026-01-19 09:00:00",
                  "information_grade": "declared_daily_open", "origin": origin},
        state=TraderState.create(book="cash=100", knowledge=knowledge, trader_note="trend"),
        request={"agent_name": "t1", "instructions": "JSON only", "message": "BOOK\n" + knowledge + "\nPANEL",
                 "model": "stub", "model_settings": None, "max_turns": 1, "tools": tools or []},
        panel_codes=["600001"], producer={"contract": "t1_orders.v1", "code_hash": "test-source"})


def test_frame_round_trip_and_nested_mutation_is_not_mutation():
    f = frame()
    copy = f.as_dict()
    copy["state"]["knowledge"] = "changed"
    assert f.as_dict()["state"]["knowledge"] == "R2: only leaders"
    assert DecisionFrame.from_dict(f.as_dict()) == f
    with pytest.raises(FrozenInstanceError):
        f._json = "{}"
    with pytest.raises(FrameError):
        DecisionFrame.from_dict(copy)


def test_separate_build_input_and_state_identity():
    a = frame()
    b = a.change_knowledge(old="only leaders", new="consider leaders", name="relax")
    left, right = a.as_dict(), b.as_dict()
    assert left["policy_build_hash"] == right["policy_build_hash"]
    assert left["state_snapshot_hash"] != right["state_snapshot_hash"]
    assert left["input_hash"] != right["input_hash"]
    assert right["provenance"]["parent_frame_hash"] == a.frame_hash
    assert left["identity"] == right["identity"]
    for field in ("model", "instructions", "tools", "model_settings", "max_turns"):
        assert left["request"][field] == right["request"][field]
    assert left["panel_codes"] == right["panel_codes"]


def test_empty_handbook_is_an_explicit_intervention():
    changed = frame().change_knowledge(old="R2: only leaders", new="", name="empty")
    assert changed.as_dict()["state"]["knowledge"] == ""
    assert changed.as_dict()["request"]["message"] == "BOOK\n\nPANEL"


@pytest.mark.parametrize("old,new", [("missing", "x"), ("only leaders", "only leaders"), ("", "x")])
def test_missing_noop_and_empty_intervention_refused(old, new):
    with pytest.raises(FrameError):
        frame().change_knowledge(old=old, new=new, name="test")


def test_ambiguous_fragment_and_ambiguous_block_refused():
    with pytest.raises(FrameError):
        frame(knowledge="repeat repeat").change_knowledge(old="repeat", new="", name="test")
    with pytest.raises(FrameError):
        frame(knowledge="BOOK").change_knowledge(old="BOOK", new="", name="test")


def test_adapter_origin_changes_capture_not_model_input():
    a, b = frame(origin="live").as_dict(), frame(origin="replay").as_dict()
    assert a["frame_hash"] != b["frame_hash"]
    for key in ("input_hash", "policy_build_hash", "state_snapshot_hash"):
        assert a[key] == b[key]


def test_run_and_trader_identity_do_not_alias():
    assert len({frame().frame_hash, frame(trader="b").frame_hash, frame(run="r2").frame_hash}) == 3


@pytest.mark.parametrize("raw", [{}, [], None, {"schema_version": 99}])
def test_invalid_envelopes_fail(raw):
    with pytest.raises(FrameError):
        DecisionFrame.from_dict(raw)


def test_nonfinite_numbers_rejected():
    value = frame().as_dict()
    value["producer"]["bad"] = float("nan")
    with pytest.raises(FrameError):
        DecisionFrame.from_dict(value)


@pytest.mark.parametrize("field,value", [("run_id", ""), ("trader_id", None),
    ("information_cutoff", "2026-01-19"), ("information_cutoff", "2026-01-20 09:00"),
    ("session_day", "bad")])
def test_required_identity_and_cutoff_are_validated(field, value):
    f = frame().as_dict()
    f["identity"][field] = value
    with pytest.raises(FrameError):
        DecisionFrame.create(identity=f["identity"], state=TraderState.create(),
                             request=f["request"], producer=f["producer"], panel_codes=f["panel_codes"])


def test_capture_saved_before_work_and_result_separate():
    with replay_as_of("2026-01-19 09:00"):
        with Capture(frame()) as c:
            conn = memory_store._get_conn()
            records = read_inputs(conn)
            assert len(records) == 1 and records[0]["frame"] == frame().as_dict()
            assert not conn.in_transaction
            c.output = {"raw": '{"orders": []}', "decision_status": "incomplete"}
    assert conn.execute("SELECT count(*) FROM decision_capture_events").fetchone()[0] == 2
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE decision_capture_events SET payload_json='{}'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM decision_capture_events")


@pytest.mark.parametrize("exc", [TimeoutError(), asyncio.CancelledError(), RuntimeError()])
def test_failure_is_a_terminal_observation_not_an_abstention(exc):
    with replay_as_of("2026-01-19 09:00"):
        with pytest.raises(type(exc)):
            with Capture(frame()):
                raise exc
    conn = memory_store._get_conn()
    raw = conn.execute("SELECT payload_json FROM decision_capture_events WHERE kind='decision_output'").fetchone()[0]
    assert json.loads(raw)["status"] == "failed"
    assert json.loads(raw)["error_type"] == type(exc).__name__
    assert json.loads(raw)["output"] is None


def test_repeated_inputs_are_separate_invocations():
    with replay_as_of("2026-01-19 09:00"):
        for _ in range(2):
            with Capture(frame()) as c:
                c.output = {"raw": "same"}
    rows = read_inputs(memory_store._get_conn())
    assert len(rows) == 2
    assert rows[0]["invocation_id"] != rows[1]["invocation_id"]
    assert rows[0]["frame"] == rows[1]["frame"]


def test_read_only_export_and_identity_filter(tmp_path):
    with replay_as_of("2026-01-19 09:00"):
        for f in (frame(), frame(trader="b"), frame(run="r2")):
            with Capture(f):
                pass
    conn = memory_store._get_conn()
    rows = read_inputs(conn, trader_id="a", run_id="r1")
    assert len(rows) == 1
    db = tmp_path / "source.db"
    target = sqlite3.connect(db)
    conn.backup(target)
    target.close()
    before = db.read_bytes()
    readonly = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        assert read_inputs(readonly, invocation_id=rows[0]["invocation_id"]) == rows
    finally:
        readonly.close()
    assert db.read_bytes() == before


def test_tampered_event_rejected_even_with_unchanged_frame():
    with replay_as_of("2026-01-19 09:00"):
        with Capture(frame()):
            pass
    conn = memory_store._get_conn()
    conn.execute("DROP TRIGGER decision_capture_events_no_update")
    conn.execute("UPDATE decision_capture_events SET run_id='other' WHERE kind='decision_input'")
    with pytest.raises(FrameError):
        read_inputs(conn)


def test_missing_legacy_capture_table_stays_missing():
    conn = sqlite3.connect(":memory:")
    assert read_inputs(conn) == []
    assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_capture_does_not_commit_unrelated_business_transaction():
    conn = memory_store._get_conn()
    conn.execute("CREATE TABLE business_test (v INTEGER)")
    conn.execute("INSERT INTO business_test VALUES (1)")
    with replay_as_of("2026-01-19 09:00"):
        with pytest.raises(FrameError):
            with Capture(frame()):
                pytest.fail("Unrelated transaction committed")
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM business_test").fetchone()[0] == 0


def test_capture_insert_failure_rolls_back_its_own_transaction():
    with replay_as_of("2026-01-19 09:00"):
        with Capture(frame()) as capture:
            conn = memory_store._get_conn()
            with pytest.raises(sqlite3.IntegrityError):
                capture._append("decision_input", {"duplicate": True})
            assert not conn.in_transaction
            capture.output = {"raw": "valid answer"}
    assert conn.execute("SELECT count(*) FROM decision_capture_events").fetchone()[0] == 2


def test_a_capture_cannot_reuse_an_old_answer():
    with replay_as_of("2026-01-19 09:00"):
        capture = Capture(frame())
        with capture:
            capture.output = {"raw": "first answer"}
        with pytest.raises(FrameError):
            with capture:
                pass


def test_frame_records_large_existing_turn_budget_without_imposing_new_policy():
    f = frame().as_dict()
    f["request"]["max_turns"] = 1000
    got = DecisionFrame.create(identity=f["identity"], state=TraderState.create(),
        request=f["request"], panel_codes=f["panel_codes"], producer=f["producer"])
    assert got.as_dict()["request"]["max_turns"] == 1000
