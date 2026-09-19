"""The first real selection gene is shared by live and Dream paths."""

import sqlite3

import pytest

from alpha_agents.data import memory_store, opportunity_journal as OJ
from alpha_agents.data import opportunity_outcomes as OO
from alpha_agents.data import policy_registry as PR, scoring, selection_policy as SP
from alpha_agents.evolution import dream_selection as DS
from alpha_agents.evolution import dream_world as DW


def test_half_share_preserves_the_historical_alternation():
    change = ["c1", "c2", "c3"]
    turnover = ["t1", "t2", "t3"]
    assert SP.weighted_merge(
        change, turnover, total=6,
        params={"selection_rank": {"change_share": 0.5}},
    ) == ["c1", "t1", "c2", "t2", "c3", "t3"]


def test_higher_change_share_changes_the_same_live_merge():
    change = ["c1", "c2", "c3", "c4"]
    turnover = ["t1", "t2", "t3", "t4"]
    got = SP.weighted_merge(
        change, turnover, total=5,
        params={"selection_rank": {"change_share": 0.7}},
    )
    assert got.count("c1") == 1
    assert sum(x.startswith("c") for x in got) > sum(
        x.startswith("t") for x in got)


def test_out_of_range_share_fails_closed():
    with pytest.raises(SP.SelectionPolicyError):
        SP.change_share({"selection_rank": {"change_share": 1.2}})


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
        "rules": {"minimum_samples": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, volume INTEGER, PRIMARY KEY(code,date))")
    dates = [
        "2025-12-01", "2025-12-02", "2025-12-03", "2025-12-04",
        "2025-12-05", "2025-12-08", "2025-12-09", "2025-12-10",
        "2025-12-11", "2025-12-12", "2025-12-15", "2025-12-16",
        "2025-12-17", "2025-12-18", "2025-12-19", "2025-12-22",
        "2025-12-23", "2025-12-24", "2025-12-25", "2025-12-26",
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
        "2026-01-09", "2026-01-12",
    ]
    paths = {
        "600001": [10, 10.1, 10.2, 10.3, 10.4, 10.5],
        "600002": [10, 10.5, 11.0, 11.5, 12.0, 12.5],
        "600003": [10, 9.9, 9.8, 9.7, 9.6, 9.5],
        "600004": [10, 10.3, 10.6, 10.9, 11.2, 11.5],
    }
    for code in paths:
        for day in dates[:20]:
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                (code, day, 10, 10.1, 9.9, 10, 1_000_000))
        for day, close in zip(dates[20:], paths[code]):
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                (code, day, 10 if day == "2026-01-05" else close,
                 close * 1.01, close * 0.99, close, 1_000_000))
    conn.commit()
    return conn


def test_dream_executes_the_same_selection_gene(store):
    parent = PR.freeze(
        sources=_sources(0.5), created_by="test",
        reason="parent", frozen_at="2026-01-01")
    variant = PR.freeze(
        sources=_sources(0.8), parent_id=parent, created_by="test",
        reason="change-heavy", frozen_at="2026-01-02")

    OJ.record_decision(
        run_id="r1", trader_id="t", day="2026-01-05", phase="open",
        information_cutoff="2026-01-05 09:00:00",
        panel=[
            {"code": "600001", "change_pct": 4.0, "turnover_rate": 1.0},
            {"code": "600002", "change_pct": 1.0, "turnover_rate": 9.0},
        ],
        orders=[{"code": "600001", "reason": "pick"}],
        refusals=[], research={"deep_dive_names": ["600001"]},
        parse_error=None, raw="{}",
        context={
            "ranking_day": "2025-12-26",
            "panel_limit": 2,
            "candidate_pool": [
                {"code": "600001", "change_pct": 4.0, "turnover_rate": 1.0},
                {"code": "600002", "change_pct": 1.0, "turnover_rate": 9.0},
                {"code": "600003", "change_pct": 3.0, "turnover_rate": 2.0},
                {"code": "600004", "change_pct": 2.0, "turnover_rate": 8.0},
            ],
        },
        conn=store)

    history = _history()
    OO.sweep(conn=store, history_conn=history)
    world = DW.build_opportunity_world(
        start="2026-01-01", end="2026-01-31", conn=store)
    got = DS.panel_policy_counterfactual(
        world, parent_version_id=parent, variant_version_id=variant,
        history_conn=history)

    assert got["comparable_sets"] == 1
    assert got["behavior_changed_sets"] == 1
    assert got["changed_genes"] == [
        "decision.selection_rank.change_share"]
    assert got["promotion_eligible"] is False
