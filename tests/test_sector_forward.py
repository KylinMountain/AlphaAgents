"""RP-10: exact, risk-vetoed Sector-First forward governance."""

from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from alpha_agents.data import memory_store, policy_registry as PR, scoring
from alpha_agents.evolution import gene_registry, sector_forward as SF


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    monkeypatch.setattr(
        SF, "_utc_now", lambda: "2026-09-20T00:00:00.000000+00:00")
    conn = memory_store._get_conn()
    yield conn
    conn.close()
    memory_store._local.conn = None


def _sources(share: float) -> dict:
    decision = {
        **scoring.DEFAULT_DECISION_PARAMS,
        "selection_rank": {"change_share": share},
    }
    return {
        "prompts": {"t1_decide.md": "same"},
        "model": {"agent_model": "same"},
        "retrieval": {"budget": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 50},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _versions():
    parent = PR.freeze(
        sources=_sources(0.5), created_by="test", reason="parent",
        frozen_at="2026-09-19")
    PR.install(version_id=parent, actor="test", reason="seed")
    candidate = PR.freeze(
        sources=_sources(0.8), parent_id=parent, created_by="test",
        reason="candidate", frozen_at="2026-09-20")
    return parent, candidate


def _ids(n=60):
    first = date(2026, 9, 21)
    return [f"{first + timedelta(days=i):%Y-%m-%d}/open" for i in range(n)]


def _open(store, **overrides):
    parent, candidate = _versions()
    args = {
        "parent_version_id": parent,
        "candidate_version_id": candidate,
        "sample_ids": _ids(),
        "protocol_hash": "1" * 64,
        "code_hash": "2" * 64,
        "data_hash": "3" * 64,
        "evaluator_hash": "4" * 64,
        "maximum_drawdown_pct": 12.0,
        "maximum_tail_loss_pct": 8.0,
        "maximum_concentration_pct": 30.0,
        "minimum_behavior_changes": 5,
        "conn": store,
    }
    args.update(overrides)
    return SF.open_run(**args), candidate


def _sample(sample_id, **overrides):
    result = {
        "sample_id": sample_id,
        "decision_at": f"{sample_id[:10]}T01:00:00+00:00",
        "evidence_scope": PR.SCOPE_CANDIDATE,
        "return_delta_pp": 0.2,
        "maximum_drawdown_pct": 5.0,
        "tail_loss_pct": 3.0,
        "maximum_concentration_pct": 20.0,
        "behavior_changed": True,
    }
    result.update(overrides)
    return result


def _ci(store, run, candidate):
    row = store.execute(
        "SELECT manifest_hash FROM sector_forward_runs WHERE id=?", (run,)
    ).fetchone()
    SF.record_ci(
        run_id=run, commit_hash="2" * 64,
        checks=[{"name": "tests", "conclusion": "success"},
                {"name": "policy", "conclusion": "success"}],
        manifest_hash=row["manifest_hash"],
        candidate_hash=PR.get_version(candidate)["content_hash"], conn=store)


def test_49_plus_10_seals_exactly_the_preregistered_50(store):
    run, _candidate = _open(store)
    ids = _ids()
    first = [_sample(sample_id) for sample_id in ids[:49]]
    assert SF.ingest(run_id=run, samples=first, conn=store)["sealed"] is False

    got = SF.ingest(
        run_id=run, samples=[_sample(sample_id) for sample_id in ids[49:59]],
        conn=store)
    assert got["sealed"] is True
    assert got["sample_count"] == 50
    assert got["new_rows"] == 1
    stored = store.execute(
        "SELECT sample_id FROM sector_forward_rows WHERE run_id=? ORDER BY ordinal",
        (run,)).fetchall()
    assert [row["sample_id"] for row in stored] == ids[:50]


def test_duplicate_and_out_of_order_delivery_has_stable_seal(store):
    run, _candidate = _open(store)
    ids = _ids()[:50]
    batch = [_sample(sample_id) for sample_id in reversed(ids)]
    batch += [_sample(ids[0]), _sample(ids[1])]
    SF.ingest(run_id=run, samples=batch, conn=store)
    before = store.execute(
        "SELECT summary_hash FROM sector_forward_seals WHERE run_id=?", (run,)
    ).fetchone()["summary_hash"]
    assert SF.ingest(run_id=run, samples=batch, conn=store)["new_rows"] == 0
    after = store.execute(
        "SELECT summary_hash FROM sector_forward_seals WHERE run_id=?", (run,)
    ).fetchone()["summary_hash"]
    assert before == after


def test_concurrent_duplicate_batches_preserve_the_same_exact_seal(store):
    run, _candidate = _open(store)
    ids = _ids()[:50]
    database = store.execute("PRAGMA database_list").fetchone()[2]

    def deliver(batch):
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return SF.ingest(run_id=run, samples=batch, conn=conn)
        finally:
            conn.close()

    forward = [_sample(sample_id) for sample_id in ids]
    reverse = list(reversed(forward))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(deliver, (forward, reverse)))

    assert any(result["sealed"] for result in results)
    rows = store.execute(
        "SELECT sample_id FROM sector_forward_rows WHERE run_id=? ORDER BY ordinal",
        (run,)).fetchall()
    assert [row["sample_id"] for row in rows] == ids
    assert store.execute(
        "SELECT COUNT(*) FROM sector_forward_seals WHERE run_id=?", (run,)
    ).fetchone()[0] == 1


def test_observation_only_and_unplanned_samples_fail_closed(store):
    run, _candidate = _open(store)
    with pytest.raises(SF.SectorForwardError, match="observation-only"):
        SF.ingest(
            run_id=run,
            samples=[_sample(_ids()[0], evidence_scope="observation_only")],
            conn=store)
    with pytest.raises(SF.SectorForwardError, match="not preregistered"):
        SF.ingest(
            run_id=run, samples=[_sample("2030-01-01/surprise")], conn=store)


def test_ci_is_hash_bound_and_all_checks_must_pass(store):
    run, candidate = _open(store)
    row = store.execute(
        "SELECT manifest_hash FROM sector_forward_runs WHERE id=?", (run,)
    ).fetchone()
    with pytest.raises(SF.SectorForwardError, match="every preregistered"):
        SF.record_ci(
            run_id=run, commit_hash="2" * 64,
            checks=[{"name": "tests", "conclusion": "failure"}],
            manifest_hash=row["manifest_hash"],
            candidate_hash=PR.get_version(candidate)["content_hash"], conn=store)
    with pytest.raises(SF.SectorForwardError, match="another manifest"):
        SF.record_ci(
            run_id=run, commit_hash="2" * 64,
            checks=[{"name": "tests", "conclusion": "success"}],
            manifest_hash="0" * 64,
            candidate_hash=PR.get_version(candidate)["content_hash"], conn=store)


def test_positive_return_is_rejected_by_drawdown_veto(store):
    run, candidate = _open(store)
    samples = [_sample(sample_id) for sample_id in _ids()[:50]]
    samples[7]["maximum_drawdown_pct"] = 12.1
    SF.ingest(run_id=run, samples=samples, conn=store)
    _ci(store, run, candidate)
    result = SF.evaluate(run, store)
    assert result["mean_diff"] > 0
    assert result["outcome"] == "reject"
    assert result["risk_breaches"]


def test_complete_safe_forward_evidence_gets_one_persisted_verdict(store):
    run, candidate = _open(store)
    SF.ingest(
        run_id=run, samples=[_sample(sample_id) for sample_id in _ids()[:50]],
        conn=store)
    _ci(store, run, candidate)
    verdict = SF.run_gate(run, store)
    assert verdict["outcome"] == "promote"
    assert verdict["n"] == 50
    assert verdict["ci_artifact_hash"]
    with pytest.raises(SF.SectorForwardError, match="already has a verdict"):
        SF.run_gate(run, store)


def test_unknown_future_child_is_not_covered_by_a_parent_prefix():
    with pytest.raises(gene_registry.GeneRegistryError, match="unknown"):
        gene_registry.assert_exact_coverage(
            ["decision.selection_rank.future_magic"],
            gene_registry.SELECTION_RANK_GENES,
            actor="selection evaluator")
