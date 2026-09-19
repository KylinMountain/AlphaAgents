"""Immutable experiment manifests and one-look stopping."""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store, policy_registry as PR, scoring
from alpha_agents.evolution import holdout_gate as HG
from alpha_agents.evolution import shadow as SH


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


def _sources(decision):
    return {
        "prompts": {"morning_scan.md": "same"},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _experiment(store):
    incumbent = PR.freeze(
        sources=_sources(dict(scoring.DEFAULT_DECISION_PARAMS)),
        created_by="kylin", reason="incumbent", frozen_at="2026-06-01")
    PR.install(version_id=incumbent, actor="kylin", reason="first")
    priors = {
        **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
        "high": 0.72,
    }
    decision = {
        **scoring.DEFAULT_DECISION_PARAMS,
        "confidence_priors": priors,
    }
    target = PR.freeze(
        sources=_sources(decision), parent_id=incumbent,
        created_by="kylin", reason="candidate", frozen_at="2026-06-01")
    run = SH.open_run(
        policy_version_id=target, reason="measure it", report_type="morning",
        producer=SH.CANDIDATE_NAME, opened_at="2026-06-01")
    return incumbent, target, run


def _pair(store, run, day, code, champ_brier=0.30):
    pred = memory_store.save_prediction(
        day, "morning", code, "X", "看多", "high", "t", 1.0, "reason",
        prob=0.6, trader_id="default", horizon_days=5)
    store.execute(
        "UPDATE predictions SET brier=?, scored_at=? WHERE id=?",
        (champ_brier, day + " 16:00:00", pred))
    store.commit()
    SH.emit_for_date(run, day, panel=[code], horizon_days=5)


def _grade(monkeypatch, as_of="2026-07-15", brier=0.10):
    monkeypatch.setattr(
        scoring, "evidence_window_closed",
        lambda entry_date, horizon=5: True)
    monkeypatch.setattr(
        scoring, "score_prediction",
        lambda code, entry_date, prob, horizon=None: {
            "brier": brier,
            "log_score": 0.2,
            "excess_return": 0.01,
            "residual_alpha": 0.005,
            "outcome": True,
            "scored_at": as_of + " 16:00:00",
        })


class TestManifest:
    def test_open_run_freezes_the_experiment_question(self, store):
        incumbent, target, run = _experiment(store)
        manifest = SH.manifest_for_run(run)

        assert manifest["reference_version_id"] == incumbent
        assert manifest["policy_version_id"] == target
        assert manifest["changed_genes"] == [
            "decision.confidence_priors.high"]
        assert manifest["observed_genes"] == [
            "decision.confidence_priors"]
        assert manifest["minimum_samples"] == HG.MIN_VALIDATION_SAMPLES
        assert manifest["stopping_rule"] == (
            "one_verdict_at_or_after_minimum_paired_samples")

    def test_manifest_is_append_only(self, store):
        _, _, run = _experiment(store)
        manifest = SH.manifest_for_run(run)
        with pytest.raises(sqlite3.DatabaseError):
            store.execute(
                "UPDATE shadow_manifests SET payload_json='{}' WHERE id=?",
                (manifest["id"],))

    def test_sample_floor_does_not_drift_after_open(self, store, monkeypatch):
        _, _, run = _experiment(store)
        assert SH.coverage(run)["runs"][0]["needed"] == 20
        monkeypatch.setattr(HG, "MIN_VALIDATION_SAMPLES", 999)
        assert SH.coverage(run)["runs"][0]["needed"] == 20


class TestSealing:
    def test_early_look_does_not_seal(self, store, monkeypatch):
        _, target, run = _experiment(store)
        _pair(store, run, "2026-06-02", "600001")
        _grade(monkeypatch)
        SH.score_due(as_of="2026-07-15")

        got = HG.run_gate(target, report_type="morning", today="2026-07-15")
        assert got["outcome"] == "insufficient"
        row = SH.get_run(run)
        assert row["status"] == "open"
        assert row["sealed_at"] is None

    def test_final_verdict_seals_and_cannot_be_looked_at_again(
            self, store, monkeypatch):
        _, target, run = _experiment(store)
        for i in range(20):
            _pair(store, run, f"2026-06-{i + 2:02d}", f"C{i:03d}")
        _grade(monkeypatch)
        SH.score_due(as_of="2026-07-15")

        got = HG.run_gate(target, report_type="morning", today="2026-07-15")
        assert got["outcome"] == "promote"
        assert got["gate_decision_id"] is not None

        row = SH.get_run(run)
        assert row["status"] == "closed"
        assert row["sealed_at"] == "2026-07-15"
        assert row["gate_decision_id"] == got["gate_decision_id"]

        with pytest.raises(HG.GateError, match="optional stopping"):
            HG.run_gate(target, report_type="morning", today="2026-07-16")
        with pytest.raises(SH.ShadowError, match="closed"):
            SH.emit_for_date(run, "2026-07-16", panel=["600999"])
