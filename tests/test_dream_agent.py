"""Dream Agent replays fixed historical evidence without promoting itself."""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store, policy_registry as PR, scoring
from alpha_agents.evolution import dream_agent as DA
from alpha_agents.evolution import dream_world as DW


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


def _sources(decision: dict) -> dict:
    return {
        "prompts": {"t1_decide.md": "same"},
        "model": {"agent_model": "same"},
        "retrieval": {"budget": 400},
        "rules": {"minimum_samples": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _versions(store):
    parent = PR.freeze(
        sources=_sources(dict(scoring.DEFAULT_DECISION_PARAMS)),
        created_by="test", reason="parent", frozen_at="2026-01-01")
    PR.install(version_id=parent, actor="test", reason="seed")

    better_priors = {
        **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
        "high": 0.80,
    }
    better = PR.freeze(
        sources=_sources({
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": better_priors,
        }),
        parent_id=parent, created_by="test",
        reason="better calibration candidate", frozen_at="2026-02-01")

    worse_priors = {
        **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
        "high": 0.40,
    }
    worse = PR.freeze(
        sources=_sources({
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": worse_priors,
        }),
        parent_id=parent, created_by="test",
        reason="worse calibration candidate", frozen_at="2026-02-01")
    return parent, better, worse


def _seed_world(store, n=25):
    for i in range(n):
        code = f"{600000 + i}"
        pred_id = memory_store.save_prediction(
            "2026-01-15", "morning", code, code, "看多", "high",
            "theme", 10.0, "historical call", prob=0.58,
            trader_id="default", horizon_days=5)
        store.execute(
            "UPDATE predictions SET hit=1, brier=?, scored_at=? WHERE id=?",
            (scoring.brier_score(0.58, True),
             "2026-01-30 10:00:00", pred_id))
    store.commit()
    return DW.build_prediction_world(
        start="2026-01-01", end="2026-01-31",
        report_type="morning", conn=store)


class TestDreamWorld:
    def test_it_freezes_only_scored_historical_rows(self, store):
        world = _seed_world(store, n=3)
        assert world.n == 3
        assert world.opportunity_scope == "champion_forecast_panel_only"
        assert world.evidence_scope == "historical_dream_only"
        assert len(world.world_hash) == 64

    def test_the_same_rows_make_the_same_world(self, store):
        first = _seed_world(store, n=3)
        second = DW.build_prediction_world(
            start="2026-01-01", end="2026-01-31",
            report_type="morning", conn=store)
        assert first.world_hash == second.world_hash


class TestDreamExperimentContract:
    def test_the_evaluator_must_observe_every_changed_gene(self, store):
        parent, _, _ = _versions(store)
        gate = {
            **scoring.DEFAULT_DECISION_PARAMS["theme_gate"],
            "w_rel": 0.40,
        }
        variant = PR.freeze(
            sources=_sources({
                **scoring.DEFAULT_DECISION_PARAMS,
                "theme_gate": gate,
            }),
            parent_id=parent, created_by="test",
            reason="wrong evaluator candidate", frozen_at="2026-02-01")
        world = _seed_world(store)

        with pytest.raises(DA.DreamError, match="theme_gate.w_rel"):
            DA.evaluate_variant(
                world=world, parent_version_id=parent,
                variant_version_id=variant, conn=store)

    def test_a_confidence_gene_can_survive_on_the_same_world(self, store):
        parent, better, _ = _versions(store)
        world = _seed_world(store)

        got = DA.evaluate_variant(
            world=world, parent_version_id=parent,
            variant_version_id=better, conn=store)
        assert got["result"]["outcome"] == DA.SURVIVOR
        assert got["result"]["delta_brier"] < 0
        assert got["result"]["promotion_eligible"] is False
        assert got["manifest"]["changed_genes"] == [
            "decision.confidence_priors.high"]

    def test_no_improvement_is_retired_not_promoted(self, store):
        parent, _, worse = _versions(store)
        world = _seed_world(store)

        got = DA.evaluate_variant(
            world=world, parent_version_id=parent,
            variant_version_id=worse, conn=store)
        assert got["result"]["outcome"] == DA.RETIRE
        assert got["result"]["delta_brier"] > 0
        assert got["result"]["promotion_eligible"] is False

    def test_too_few_rows_are_insufficient(self, store):
        parent, better, _ = _versions(store)
        world = _seed_world(store, n=3)

        got = DA.evaluate_variant(
            world=world, parent_version_id=parent,
            variant_version_id=better, minimum_samples=20, conn=store)
        assert got["result"]["outcome"] == DA.INSUFFICIENT


class TestDreamAgent:
    def test_multiple_variants_share_one_world_and_move_no_pointer(self, store):
        parent, better, worse = _versions(store)
        world = _seed_world(store)
        before = PR.active()["version_id"]

        got = DA.DreamAgent().screen(
            world=world, parent_version_id=parent,
            variant_version_ids=[better, worse], conn=store)

        assert got["survivors"] == [better]
        assert got["retired"] == [worse]
        assert {x["result"]["world_hash"] for x in got["experiments"]} == {
            world.world_hash}
        assert got["promotion_eligible"] is False
        assert PR.active()["version_id"] == before == parent

    def test_the_record_is_append_only(self, store):
        parent, better, _ = _versions(store)
        world = _seed_world(store)
        DA.evaluate_variant(
            world=world, parent_version_id=parent,
            variant_version_id=better, conn=store)

        with pytest.raises(sqlite3.DatabaseError):
            store.execute(
                "UPDATE dream_experiments SET evaluator='changed' WHERE id=1")
