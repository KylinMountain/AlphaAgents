"""Direction Opportunity Journal feeds an immutable sector Dream world."""

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import sector_selection as SS
from alpha_agents.data import theme_opportunity_journal as J
from alpha_agents.data import theme_opportunity_outcomes as O
from alpha_agents.evolution import dream_direction as D


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
def membership():
    return SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-04 23:59:59",
        source="fixture",
        sector_type="concept",
        point_in_time=True,
        members={
            "AI": ("600001", "600002"),
            "存储": ("600003", "600004"),
            "医药": ("600005", "600006"),
        },
    )


@pytest.fixture()
def history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE daily_kline ("
        "code TEXT, date TEXT, open REAL, close REAL)")
    dates = [
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
        "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14",
        "2026-01-15", "2026-01-16", "2026-01-19",
    ]
    daily_step = {
        "600001": 0.02,
        "600002": 0.01,
        "600003": 0.05,
        "600004": 0.04,
        "600005": -0.01,
        "600006": 0.00,
    }
    for code, step in daily_step.items():
        for i, day in enumerate(dates):
            close = 10.0 * (1.0 + step * i)
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?)",
                (code, day, 10.0 if i == 0 else close, close))
    conn.commit()
    return conn


def _snapshot(membership, sector_id, rank):
    return {
        "sector_id": sector_id,
        "rank": rank,
        "membership_snapshot_id": membership.snapshot_id,
        "membership_hash": membership.content_hash,
        "relative_returns_pct": {"5d": 1.0 / rank},
    }


def _journal(store, membership):
    return J.record(
        run_id="r1",
        trader_id="t1",
        day="2026-01-05",
        phase="open",
        information_cutoff="2026-01-05 09:00:00",
        architecture="sector_first_v0",
        snapshots=[
            _snapshot(membership, "AI", 1),
            _snapshot(membership, "存储", 2),
            _snapshot(membership, "医药", 3),
        ],
        shortlist=["AI", "存储"],
        selected=["AI"],
        research={"themes": ["AI"]},
        conn=store,
    )


def test_sector_outcomes_wait_for_market_horizon_and_label_all(
        store, membership, history):
    _journal(store, membership)
    got = O.sweep(
        membership_archive=(membership,),
        conn=store,
        history_conn=history,
    )
    assert got["written"] == 3
    assert got["pending"] == 0
    assert O.coverage(store)["coverage"] == 1.0


def test_outcome_preserves_member_coverage_and_head_dependence(
        membership, history):
    got = O.compute(
        sector_id="存储",
        membership_snapshot=membership,
        day="2026-01-05",
        phase="open",
        history_conn=history,
    )
    assert got is not None
    assert got["coverage"]["5"]["ratio"] == 1.0
    assert got["forward_median_return_pct"]["5"] > 0
    assert (
        got["ex_top1_forward_median_return_pct"]["5"]
        <= got["forward_median_return_pct"]["5"]
    )


def test_direction_dream_exposes_missed_better_sector(
        store, membership, history):
    _journal(store, membership)
    O.sweep(
        membership_archive=(membership,),
        conn=store,
        history_conn=history,
    )
    world = D.build_world(
        start="2026-01-01",
        end="2026-01-31",
        conn=store,
    )
    got = D.selection_skill(world, horizon=5)
    assert world.n_sets == 1
    assert got["selected"]["n"] == 1
    assert got["mean_regret"] > 0
    assert got["selected_lift_vs_all"] is not None
    assert got["promotion_eligible"] is False


def test_transparent_rank_baseline_uses_the_same_world(
        store, membership, history):
    _journal(store, membership)
    O.sweep(
        membership_archive=(membership,),
        conn=store,
        history_conn=history,
    )
    world = D.build_world(
        start="2026-01-01",
        end="2026-01-31",
        conn=store,
    )
    got = D.transparent_rank_baseline(
        world, horizon=5, top_k=2)
    assert got["comparable_sets"] == 1
    assert got["transparent_rank"]["n"] == 2
    assert got["agent_selected"]["n"] == 1
    assert got["promotion_eligible"] is False


def test_outcome_history_is_append_only(store, membership, history):
    _journal(store, membership)
    O.sweep(
        membership_archive=(membership,),
        conn=store,
        history_conn=history,
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.execute(
            "UPDATE theme_opportunity_outcomes "
            "SET sector_id='x' WHERE id=1")
