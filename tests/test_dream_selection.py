"""Opportunity outcomes feed an immutable selection DreamWorld."""

import sqlite3

import pytest

from alpha_agents.data import memory_store, opportunity_journal as OJ
from alpha_agents.data import opportunity_outcomes as OO
from alpha_agents.evolution import dream_selection as DS
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


@pytest.fixture()
def history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, PRIMARY KEY(code,date))")
    dates = [
        "2026-01-05", "2026-01-06", "2026-01-07",
        "2026-01-08", "2026-01-09", "2026-01-12",
    ]
    closes = {
        "600001": [10, 10.2, 10.3, 10.4, 10.5, 10.6],
        "600002": [10, 10.5, 11.0, 11.5, 12.0, 12.5],
        "600003": [10, 9.9, 9.8, 9.7, 9.6, 9.5],
    }
    for code, values in closes.items():
        for day, close in zip(dates, values):
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?)",
                (code, day, 10.0 if day == dates[0] else close,
                 close * 1.02, close * 0.98, close))
    conn.commit()
    return conn


def _journal(store):
    return OJ.record_decision(
        run_id="dream-r1", trader_id="t1", day="2026-01-05", phase="open",
        information_cutoff="2026-01-05 09:00:00",
        panel=[
            {"code": "600001", "change_pct": 3.0, "turnover_rate": 5.0},
            {"code": "600002", "change_pct": 2.0, "turnover_rate": 9.0},
            {"code": "600003", "change_pct": 1.0, "turnover_rate": 2.0},
        ],
        orders=[{"code": "600001", "reason": "picked A"}],
        refusals=[],
        research={"deep_dive_names": ["600001", "600002"]},
        parse_error=None, raw="{}",
        context={"run_theme": "AI", "ranking_day": "2026-01-02"},
        conn=store)


def test_sweep_waits_for_complete_horizon_and_labels_all_items(store, history):
    _journal(store)
    got = OO.sweep(conn=store, history_conn=history)
    assert got["written"] == 3
    assert got["pending"] == 0
    assert OO.coverage(store)["coverage"] == 1.0


def test_opportunity_world_preserves_selected_and_alternatives(store, history):
    _journal(store)
    OO.sweep(conn=store, history_conn=history)
    world = DW.build_opportunity_world(
        start="2026-01-01", end="2026-01-31", conn=store)
    assert world.n_sets == 1
    assert world.n_items == 3
    group = world.sets[0]
    assert group.context["run_theme"] == "AI"
    assert [x.code for x in group.items if x.selected] == ["600001"]
    assert {x.status for x in group.items} == {
        "agent_selected", "researched_not_selected", "offered_not_researched"}


def test_selection_skill_exposes_opportunity_cost(store, history):
    _journal(store)
    OO.sweep(conn=store, history_conn=history)
    world = DW.build_opportunity_world(
        start="2026-01-01", end="2026-01-31", conn=store)
    got = DS.selection_skill(world, horizon=5)
    assert got["selected"]["n"] == 1
    assert got["mean_regret"] > 0
    assert got["selected_lift_vs_panel_mean"] is not None
    assert got["promotion_eligible"] is False


def test_turnover_baseline_can_choose_the_better_alternative(store, history):
    _journal(store)
    OO.sweep(conn=store, history_conn=history)
    world = DW.build_opportunity_world(
        start="2026-01-01", end="2026-01-31", conn=store)
    got = DS.ranking_baseline(
        world, feature="turnover_rate", horizon=5, top_k=1)
    assert got["baseline"]["mean"] > got["champion_selected"]["mean"]
    assert got["promotion_eligible"] is False


def test_outcome_history_is_append_only(store, history):
    _journal(store)
    OO.sweep(conn=store, history_conn=history)
    with pytest.raises(sqlite3.DatabaseError):
        store.execute(
            "UPDATE opportunity_outcomes SET code='x' WHERE id=1")
