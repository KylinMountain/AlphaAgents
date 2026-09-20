"""Selection gate turns one sealed forward sample into one persisted verdict."""

import json
import sqlite3

import pytest

from alpha_agents.data import memory_store, policy_registry as PR, scoring
from alpha_agents.evolution import holdout_gate
from alpha_agents.evolution import selection_gate as G
from alpha_agents.evolution import selection_shadow as S


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    current = getattr(memory_store._local, "conn", None)
    if current is not None:
        current.close()
    memory_store._local.conn = None


def _sources(share):
    decision = dict(scoring.DEFAULT_DECISION_PARAMS)
    decision["selection_rank"] = {"change_share": share}
    return {
        "prompts": {"t1_decide.md": "same"},
        "model": {"agent_model": "same"},
        "retrieval": {"budget": 400},
        "rules": {
            "holdout_gate.MIN_VALIDATION_SAMPLES":
                holdout_gate.MIN_VALIDATION_SAMPLES,
        },
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _seed_sealed_run(store, deltas, *, changed=None, minimum_sets=20):
    parent = PR.freeze(
        sources=_sources(0.5), created_by="test",
        reason="parent", frozen_at="2026-01-01")
    variant = PR.freeze(
        sources=_sources(0.8), parent_id=parent, created_by="test",
        reason="challenger", frozen_at="2026-01-02")
    run_id = S.open_run(
        parent_version_id=parent, variant_version_id=variant,
        opened_on="2026-01-03", minimum_sets=minimum_sets,
        minimum_behavior_changes=5, minimum_mean_delta=0.0, conn=store)
    S.init_schema(store)
    changed = len(deltas) if changed is None else changed
    for i, delta in enumerate(deltas):
        result = {
            "variant_minus_parent_mean": delta,
            "behavior_changed_sets": 1 if i < changed else 0,
        }
        store.execute(
            "INSERT INTO selection_shadow_rows "
            "(run_id,opportunity_set_id,day,result_json,result_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, i + 1, f"2026-02-{(i % 28) + 1:02d}",
             json.dumps(result), f"h{i}", "2026-03-01"))
    run = S.get_run(run_id, store)
    S._seal(run, store)
    store.commit()
    return parent, variant, run_id


def test_positive_forward_delta_produces_candidate_gate(store):
    _, variant, run = _seed_sealed_run(store, [1.0] * 20)
    got = G.run_gate(run, today="2026-03-10", conn=store)
    assert got["outcome"] == "promote"
    assert got["policy_version_id"] == variant
    assert got["evidence_scope"] == PR.SCOPE_CANDIDATE
    assert got["n"] == 20
    assert got["id"] > 0


def test_inert_gene_abstains_even_if_mean_is_positive(store):
    _, _, run = _seed_sealed_run(
        store, [1.0] * 20, changed=2)
    got = G.evaluate(run, store)
    assert got["outcome"] == "insufficient"
    assert got["abstained"] is True


def test_negative_delta_is_rejected(store):
    _, _, run = _seed_sealed_run(store, [-1.0] * 20)
    got = G.evaluate(run, store)
    assert got["outcome"] == "reject"
    assert got["abstained"] is False


def test_the_same_seal_cannot_get_a_second_verdict(store):
    _, _, run = _seed_sealed_run(store, [1.0] * 20)
    G.run_gate(run, today="2026-03-10", conn=store)
    with pytest.raises(G.SelectionGateError, match="already has gate verdict"):
        G.run_gate(run, today="2026-03-11", conn=store)
