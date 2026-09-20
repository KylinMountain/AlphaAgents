"""RP-09: isolated forward evidence is append-only and observation-only."""

from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from alpha_agents.evolution.forward_evidence import (
    ForwardEvidenceError,
    ForwardEvidenceStore,
    RESULT_STATES,
)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_capture_is_idempotent_and_revisions_append(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    first = store.capture_source(
        source="news",
        source_key="article-1",
        collector_version="collector-v1",
        result_state="success",
        payload={"title": "v1"},
        source_published_at="2026-09-20T09:00:00+08:00",
        source_available_at="2026-09-20T09:01:00+08:00",
        original_timezone="Asia/Taipei",
    )
    retry = store.capture_source(
        source="news",
        source_key="article-1",
        collector_version="collector-v1",
        result_state="success",
        payload={"title": "v1"},
        source_published_at="2026-09-20T09:00:00+08:00",
        source_available_at="2026-09-20T09:01:00+08:00",
        original_timezone="Asia/Taipei",
    )
    revised = store.capture_source(
        source="news",
        source_key="article-1",
        collector_version="collector-v1",
        result_state="success",
        payload={"title": "v2"},
    )

    assert retry["id"] == first["id"]
    assert revised["id"] != first["id"]
    rows = store.conn.execute(
        "SELECT id,payload_hash FROM forward_source_versions ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["payload_hash"] != rows[1]["payload_hash"]


def test_failed_capture_retry_is_also_idempotent(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    first = store.capture_source(
        source="fund_flow",
        source_key="2026-09-20:600001",
        collector_version="v1",
        result_state="timeout",
        error_class="TimeoutError",
        error_message="provider timeout",
    )
    retry = store.capture_source(
        source="fund_flow",
        source_key="2026-09-20:600001",
        collector_version="v1",
        result_state="timeout",
        error_class="TimeoutError",
        error_message="provider timeout",
    )
    assert first["id"] == retry["id"]


def test_registered_time_is_server_owned_not_opened_on(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    row = store.record_decision(
        decision_key="d1",
        opened_on="2020-01-01",
        code_ref="a" * 40,
        policy_ref="policy-v1",
        world_hash="w" * 64,
        packet_hash="p" * 64,
        model_config={"name": "m"},
        request={"prompt": "x"},
        budget={"max_calls": 1},
        result_state="abstained",
        result={"reason": "no setup"},
    )

    registered = datetime.fromisoformat(row["registered_at_utc"])
    assert registered.year == 2026
    assert row["opened_on"] == "2020-01-01"
    assert row["observation_only"] == 1


def test_decision_retry_is_idempotent_and_changed_request_is_new(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    kwargs = dict(
        decision_key="d1",
        opened_on="2026-09-21",
        code_ref="a" * 40,
        policy_ref="policy-v1",
        world_hash="w" * 64,
        packet_hash=None,
        model_config={"name": "m", "temperature": 0},
        budget={"max_calls": 0},
        result_state="empty",
        result=[],
    )
    first = store.record_decision(request={"panel": ["600001"]}, **kwargs)
    retry = store.record_decision(request={"panel": ["600001"]}, **kwargs)
    changed = store.record_decision(request={"panel": ["600002"]}, **kwargs)

    assert retry["id"] == first["id"]
    assert changed["id"] != first["id"]


@pytest.mark.parametrize("state", sorted(RESULT_STATES))
def test_all_declared_result_states_are_recordable(tmp_path, state):
    store = ForwardEvidenceStore(tmp_path / f"{state}.db")
    row = store.record_decision(
        decision_key=state,
        opened_on="2026-09-21",
        code_ref="a" * 40,
        policy_ref="policy-v1",
        world_hash="w" * 64,
        packet_hash=None,
        model_config={"name": "m"},
        request={"state": state},
        budget={},
        result_state=state,
        result=None,
        error_class="X" if state in {
            "parse_error", "permission_denied", "timeout", "technical_error"
        } else None,
    )
    assert row["result_state"] == state
    assert row["observation_only"] == 1


def test_unknown_result_state_is_refused(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    with pytest.raises(ForwardEvidenceError, match="result_state"):
        store.capture_source(
            source="x", source_key="1", collector_version="v1",
            result_state="looks_good")


def test_credentials_are_redacted_recursively(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    source = store.capture_source(
        source="api",
        source_key="1",
        collector_version="v1",
        result_state="success",
        payload={
            "Authorization": "Bearer abc",
            "nested": {
                "api_key": "secret",
                "safe": "visible",
            },
        },
    )
    payload = json.loads(source["payload_json"])
    assert payload["Authorization"] == "[REDACTED]"
    assert payload["nested"]["api_key"] == "[REDACTED]"
    assert payload["nested"]["safe"] == "visible"

    decision = store.record_decision(
        decision_key="d",
        opened_on="2026-09-21",
        code_ref="a" * 40,
        policy_ref="p",
        world_hash="w",
        packet_hash=None,
        model_config={"token": "x", "name": "m"},
        request={"headers": {"cookie": "x"}, "prompt": "safe"},
        budget={"secret_limit": "never log me"},
        result_state="success",
        result={"ok": True},
    )
    assert "never log me" not in decision["budget_json"]
    assert "cookie" in decision["request_json"]
    assert "[REDACTED]" in decision["request_json"]


def test_append_only_triggers_block_rewrite_and_delete(tmp_path):
    store = ForwardEvidenceStore(tmp_path / "forward.db")
    row = store.capture_source(
        source="news", source_key="1", collector_version="v1",
        result_state="success", payload={"v": 1})

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute(
            "UPDATE forward_source_versions SET result_state='empty' "
            "WHERE id=?", (row["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute(
            "DELETE FROM forward_source_versions WHERE id=?", (row["id"],))


def test_disabled_capture_writes_nothing_and_keeps_history(tmp_path):
    path = tmp_path / "forward.db"
    store = ForwardEvidenceStore(path)
    existing = store.capture_source(
        source="news", source_key="1", collector_version="v1",
        result_state="success", payload={"v": 1})
    store.enabled = False

    assert store.capture_source(
        source="news", source_key="2", collector_version="v1",
        result_state="success", payload={"v": 2}) is None
    assert store.record_decision(
        decision_key="d", opened_on="2026-09-21",
        code_ref="a" * 40, policy_ref="p", world_hash="w",
        packet_hash=None, model_config={}, request={}, budget={},
        result_state="empty") is None
    assert store.conn.execute(
        "SELECT COUNT(*) FROM forward_source_versions").fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT id FROM forward_source_versions").fetchone()[0] == existing["id"]


def test_forward_store_does_not_touch_a_separate_production_db(tmp_path):
    production = tmp_path / "memory.db"
    production.write_bytes(b"production-ledger")
    before = _sha(production)

    store = ForwardEvidenceStore(tmp_path / "isolated" / "forward.db")
    store.capture_source(
        source="news", source_key="1", collector_version="v1",
        result_state="success", payload={"v": 1})

    assert _sha(production) == before
