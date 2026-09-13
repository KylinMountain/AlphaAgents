"""U5: only the pointer-selected, approved rule versions reach a prompt.

All evidence here is synthetic and private to pytest. These tests assert both
positive reachability and fail-closed behaviour; an always-empty gate is not a
passing implementation. Historical snapshots store hashes, not old payloads:
edited/deleted approved rows are blocked, not silently replaced by live text.
"""
from __future__ import annotations

import logging

import pytest

from alpha_agents.data import knowledge_snapshots as KS
from alpha_agents.data import memory_store, policy_registry as PR
from alpha_agents.evolution import feedback

TODAY = "2026-09-12"
APPROVED = "approved-rule-alpha"
NEVER_APPROVED = "never-approved-rule-beta"
LATER_APPROVED = "later-approved-rule-gamma"


def _sources(snapshot_id):
    return {
        "prompts": {"morning_scan.md": "same"},
        "model": {"agent_model": "test-model"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": snapshot_id},
    }


def _playbook(name, status="active"):
    return memory_store.create_playbook(
        name=name, pattern_json={"k": name}, today=TODAY, status=status, weight=1.0)


def _principle(text):
    return memory_store.create_trading_principle(
        principle=text, pattern_description=text, category="insight",
        action_guidance="follow the rule", evidence=[{"case": text}],
        today=TODAY, win_rate=0.6)


def _approve(*entities):
    return KS.approve(
        approved_by="test-human", reason="synthetic fixture only",
        items=[{"entity_type": kind, "entity_id": eid} for kind, eid in entities],
        approved_at=TODAY)


def _in_force(snapshot_id):
    version = PR.freeze(sources=_sources(snapshot_id), created_by="test-human",
                        reason="synthetic test", frozen_at=TODAY)
    PR.install(version_id=version, actor="test-human", reason="test bootstrap")
    return version


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    conn.close()
    memory_store._local.conn = None


@pytest.mark.parametrize("kind,create,render", [
    ("playbook", _playbook, feedback.inject_playbooks),
    ("principle", _principle, feedback.inject_principles),
])
def test_unapproved_active_row_does_not_leak(store, kind, create, render):
    approved = create(APPROVED)
    create(NEVER_APPROVED)
    _in_force(_approve((kind, approved)))
    out = render()
    assert APPROVED in out
    assert NEVER_APPROVED not in out


def test_degraded_is_not_an_approval(store):
    _playbook(NEVER_APPROVED, status="degraded")
    _in_force(_approve(("principle", _principle(APPROVED))))
    assert feedback.inject_playbooks() == ""


def test_mixed_snapshot_has_both_sections(store):
    _in_force(_approve(("playbook", _playbook(APPROVED)),
                       ("principle", _principle(LATER_APPROVED))))
    assert APPROVED in feedback.inject_playbooks()
    assert LATER_APPROVED in feedback.inject_principles()


def test_newest_snapshot_does_not_activate_itself(store):
    first = _playbook(APPROVED)
    second = _playbook(LATER_APPROVED)
    active_snapshot = _approve(("playbook", first))
    _in_force(active_snapshot)
    before = feedback.inject_playbooks()
    newest = _approve(("playbook", second))
    assert newest != active_snapshot
    assert feedback.in_force_snapshot_id() == active_snapshot
    assert feedback.inject_playbooks() == before
    assert LATER_APPROVED not in before


def test_live_collector_uses_pointer_not_newest_approval(store):
    from alpha_agents.evolution import policy_sources
    first = _playbook(APPROVED)
    active_snapshot = _approve(("playbook", first))
    _in_force(active_snapshot)
    newer = _approve(("playbook", _playbook(LATER_APPROVED)))
    assert newer != active_snapshot
    assert policy_sources.collect()["knowledge"]["snapshot_id"] == active_snapshot


def test_pointer_switch_and_rollback_change_retrieval(store):
    first = _playbook(APPROVED)
    second = _playbook(LATER_APPROVED)
    s1 = _approve(("playbook", first))
    v1 = _in_force(s1)
    s2 = _approve(("playbook", second))
    v2 = PR.freeze(sources=_sources(s2), created_by="test-human",
                   reason="synthetic candidate", frozen_at="2026-09-01")
    from alpha_agents.evolution import holdout_gate as HG
    HG.record_gate_decision(f"trader#{v2}", {
        "policy_version_id": v2, "outcome": "promote", "promote": True,
        "abstained": False, "validation_days": 9, "n": 30,
        "reason": "synthetic test fixture, not market evidence",
        "evidence_scope": PR.SCOPE_CANDIDATE,
    }, today=TODAY)
    verdict = HG.get_gate_decisions(policy_version_id=v2)[0]
    assert verdict["detail_json"].find("synthetic test fixture") >= 0
    PR.approve(version_id=v2, approved_by="test-human", reason="synthetic",
               gate_decision=verdict, sources=_sources(s2))
    assert LATER_APPROVED not in feedback.inject_playbooks()
    PR.promote(version_id=v2, actor="test-human", reason="synthetic",
               sources=_sources(s2), expected_seq=1)
    out = feedback.inject_playbooks()
    assert LATER_APPROVED in out and APPROVED not in out
    PR.rollback(to_version_id=v1, actor="test-human", reason="synthetic",
                sources=_sources(s1), expected_seq=2)
    assert APPROVED in feedback.inject_playbooks()
    assert LATER_APPROVED not in feedback.inject_playbooks()


@pytest.mark.parametrize("snapshot_id", [None, 999999, "not-an-id", -1])
def test_missing_or_malformed_snapshot_fails_closed(store, caplog, snapshot_id):
    _playbook(NEVER_APPROVED)
    _in_force(snapshot_id)
    with caplog.at_level(logging.WARNING):
        out = feedback.inject_playbooks()
    assert NEVER_APPROVED not in out
    assert "未生效" in out
    assert caplog.records


def test_no_pointer_fails_closed_and_logs(store, caplog):
    _playbook(NEVER_APPROVED)
    with caplog.at_level(logging.WARNING):
        out = feedback.inject_playbooks()
    assert "未生效" in out and NEVER_APPROVED not in out
    assert any("in force" in r.getMessage() for r in caplog.records)


def test_valid_snapshot_with_no_playbooks_is_empty_not_degraded(store, caplog):
    _in_force(_approve(("principle", _principle(APPROVED))))
    with caplog.at_level(logging.WARNING):
        assert feedback.inject_playbooks() == ""
    assert not caplog.records


@pytest.mark.parametrize("kind,create,render,table,column,value", [
    ("playbook", _playbook, feedback.inject_playbooks, "playbooks", "name", "UNAPPROVED EDIT"),
    ("playbook", _playbook, feedback.inject_playbooks, "playbooks", "weight", 7.0),
    ("playbook", _playbook, feedback.inject_playbooks, "playbooks", "status", "retired"),
    ("principle", _principle, feedback.inject_principles, "trading_principles", "action_guidance", "UNAPPROVED EDIT"),
    ("principle", _principle, feedback.inject_principles, "trading_principles", "status", "weakened"),
])
def test_edit_after_approval_never_reaches_prompt(
        store, caplog, kind, create, render, table, column, value):
    eid = create(APPROVED)
    _in_force(_approve((kind, eid)))
    assert APPROVED in render()
    store.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, eid))
    store.commit()
    with caplog.at_level(logging.WARNING):
        out = render()
    assert APPROVED not in out and "UNAPPROVED EDIT" not in out
    assert "未生效" in out
    assert any("drifted" in r.getMessage() for r in caplog.records)


def test_deleted_rule_is_not_an_empty_healthy_snapshot(store, caplog):
    eid = _playbook(APPROVED)
    _in_force(_approve(("playbook", eid)))
    store.execute("DELETE FROM playbooks WHERE id = ?", (eid,))
    store.commit()
    with caplog.at_level(logging.WARNING):
        assert "未生效" in feedback.inject_playbooks()
    assert caplog.records


def test_corrupt_snapshot_fails_closed(store, monkeypatch):
    _in_force(_approve(("playbook", _playbook(APPROVED))))
    monkeypatch.setattr(KS, "verify_snapshot", lambda _: False)
    assert APPROVED not in feedback.inject_playbooks()
    assert "未生效" in feedback.inject_playbooks()


def test_counts_cannot_change_text_order_or_budget(store):
    p1, p2 = _principle(APPROVED), _principle(LATER_APPROVED)
    b1 = _playbook(APPROVED)
    _in_force(_approve(("principle", p1), ("principle", p2), ("playbook", b1)))
    before = feedback.inject_principles(), feedback.inject_playbooks()
    assert "9999" not in "".join(before)
    store.execute("UPDATE trading_principles SET evidence_count = 9999, win_rate = 1 WHERE id = ?", (p2,))
    store.execute("UPDATE playbooks SET hit_rate = 1, wins = 9999, total_trades = 9999")
    store.commit()
    after = feedback.inject_principles(), feedback.inject_playbooks()
    assert before == after
    assert "9999" not in "".join(after) and "100%" not in "".join(after)


def test_weakened_approved_rule_has_a_marker(store):
    eid = _principle(APPROVED)
    store.execute("UPDATE trading_principles SET status = 'weakened' WHERE id = ?", (eid,))
    store.commit()
    _in_force(_approve(("principle", eid)))
    assert "[weakened]" in feedback.inject_principles()


def test_rule_fields_remain_visible(store):
    _in_force(_approve(("playbook", _playbook(APPROVED))))
    out = feedback.inject_playbooks()
    assert "status=active" in out and "weight=1.0" in out


def test_pointer_is_read_once_per_render(store, monkeypatch):
    _in_force(_approve(("playbook", _playbook(APPROVED))))
    original = feedback.in_force_snapshot_id
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(feedback, "in_force_snapshot_id", counted)
    assert APPROVED in feedback.inject_playbooks()
    assert len(calls) == 1


def test_verified_projection_excludes_all_running_counters(store):
    sid = _approve(("principle", _principle(APPROVED)))
    row = KS.verified_rows(sid, "principle")[0]
    assert set(row) == {"id", "principle", "pattern_description", "category", "action_guidance", "status"}
    with pytest.raises(ValueError):
        KS.verified_rows(sid, "unrecognised")
