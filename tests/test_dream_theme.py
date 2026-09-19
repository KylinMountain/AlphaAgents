"""Theme-gate Dream evaluation uses real selected opportunities."""

import sqlite3

import pytest

from alpha_agents.data import memory_store, opportunity_journal as OJ
from alpha_agents.data import opportunity_outcomes as OO
from alpha_agents.data import policy_registry as PR, scoring
from alpha_agents.evolution import dream_theme as DT
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


def _sources(decision):
    return {
        "prompts": {"t1_decide.md": "same"},
        "model": {"agent_model": "same"},
        "retrieval": {"budget": 400},
        "rules": {"minimum_samples": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, PRIMARY KEY(code,date))")
    dates = [
        "2026-09-14", "2026-09-15", "2026-09-16",
        "2026-09-17", "2026-09-18", "2026-09-21",
    ]
    for i, day in enumerate(dates):
        close = 10 + i
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?)",
            ("600001", day, 10.0 if i == 0 else close,
             close * 1.01, close * 0.99, close))
    conn.commit()
    return conn


def test_theme_variant_flips_a_real_selected_intent(store):
    parent = PR.freeze(
        sources=_sources(dict(scoring.DEFAULT_DECISION_PARAMS)),
        created_by="test", reason="parent", frozen_at="2026-09-01")
    PR.install(version_id=parent, actor="test", reason="seed")

    gate = {
        **scoring.DEFAULT_DECISION_PARAMS["theme_gate"],
        "admit_score": 0.56,
    }
    variant = PR.freeze(
        sources=_sources({
            **scoring.DEFAULT_DECISION_PARAMS,
            "theme_gate": gate,
        }),
        parent_id=parent, created_by="test",
        reason="stricter gate", frozen_at="2026-09-02")

    OJ.record_decision(
        run_id="r1", trader_id="t1", day="2026-09-14", phase="open",
        information_cutoff="2026-09-14 09:00:00",
        panel=[{"code": "600001", "change_pct": 2.0}],
        orders=[{"code": "600001", "reason": "pick"}],
        refusals=[], research={"deep_dive_names": ["600001"]},
        parse_error=None, raw="{}",
        context={"run_theme": "AI", "ranking_day": "2026-09-11"},
        conn=store)

    store.execute(
        "CREATE TABLE theme_score_history ("
        "id INTEGER PRIMARY KEY, theme TEXT, as_of TEXT, score REAL, "
        "flow_pct REAL, rel_pct REAL, confirm REAL, source TEXT)")
    store.execute(
        "INSERT INTO theme_score_history "
        "(theme,as_of,score,flow_pct,rel_pct,confirm,source) "
        "VALUES (?,?,?,?,?,?,?)",
        ("AI", "2026-09-14", 0.54, 0.6, 0.5, 0.45, "test"))
    store.commit()

    history = _history()
    OO.sweep(conn=store, history_conn=history)
    world = DW.build_opportunity_world(
        start="2026-09-01", end="2026-09-30", conn=store)
    got = DT.compare(
        world=world, parent_version_id=parent,
        variant_version_id=variant, conn=store)

    assert got["comparable_intents"] == 1
    assert got["behavior_flips"] == 1
    assert got["newly_blocked"]["n"] == 1
    assert got["promotion_eligible"] is False


def test_theme_evaluator_refuses_unrelated_gene(store):
    parent = PR.freeze(
        sources=_sources(dict(scoring.DEFAULT_DECISION_PARAMS)),
        created_by="test", reason="parent", frozen_at="2026-09-01")
    priors = {
        **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
        "high": 0.70,
    }
    variant = PR.freeze(
        sources=_sources({
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": priors,
        }),
        parent_id=parent, created_by="test",
        reason="not a theme gene", frozen_at="2026-09-02")
    world = DW.OpportunityDreamWorld(
        start="2026-09-01", end="2026-09-30", sets=(),
        world_hash="x" * 64)

    with pytest.raises(DT.ThemeDreamError, match="confidence_priors"):
        DT.compare(
            world=world, parent_version_id=parent,
            variant_version_id=variant, conn=store)
