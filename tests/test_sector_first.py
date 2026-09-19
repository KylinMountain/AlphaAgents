"""Sector-first S1: deterministic PIT sector facts and direction journal."""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import sector_selection as S
from alpha_agents.data import theme_opportunity_journal as J


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


def _world():
    sessions = [
        "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
        "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13",
        "2026-01-14", "2026-01-15", "2026-01-16", "2026-01-19",
        "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23",
        "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29",
        "2026-01-30",
    ]
    codes = ["600001", "600002", "600003", "600004", "600005", "600006"]
    bars = {}
    for i, day in enumerate(sessions):
        day_rows = {}
        for j, code in enumerate(codes):
            close = 10 + i * (0.05 + j * 0.015)
            day_rows[code] = {
                "close": close,
                "change_pct": (
                    0.5 + j * 0.2 if i % 2 == 0 else -0.2 + j * 0.2),
            }
        bars[day] = day_rows

    membership = S.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-30 08:30:00",
        source="fixture",
        sector_type="concept",
        point_in_time=True,
        members={
            "AI": ("600001", "600002", "600003", "600004"),
            "存储": ("600003", "600004", "600005", "600006"),
        },
    )
    return sessions, bars, membership


def test_same_world_builds_identical_hashes():
    sessions, bars, membership = _world()
    kwargs = dict(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars,
        market_codes=set(bars["2026-01-30"]),
    )
    first = S.build_sector_snapshots(**kwargs)
    second = S.build_sector_snapshots(**kwargs)
    assert [row.input_hash for row in first] == [
        row.input_hash for row in second]


def test_future_membership_is_refused():
    sessions, bars, membership = _world()
    future = S.MembershipSnapshot(
        snapshot_id="future",
        available_at="2026-02-01 00:00:00",
        source="fixture",
        sector_type=membership.sector_type,
        members=membership.members,
        point_in_time=True,
    )
    with pytest.raises(S.SectorSnapshotError, match="not available"):
        S.build_sector_snapshots(
            membership=future,
            decision_at="2026-01-30 09:00:00",
            as_of_session="2026-01-30",
            sessions=sessions,
            bars_by_day=bars)


def test_current_only_membership_is_refused_in_strict_mode():
    sessions, bars, membership = _world()
    current_only = S.MembershipSnapshot(
        snapshot_id="current",
        available_at="2026-01-01 00:00:00",
        source="stocks.db:concept_stocks",
        sector_type=membership.sector_type,
        members=membership.members,
        point_in_time=False,
    )
    with pytest.raises(S.SectorSnapshotError, match="point-in-time"):
        S.build_sector_snapshots(
            membership=current_only,
            decision_at="2026-01-30 09:00:00",
            as_of_session="2026-01-30",
            sessions=sessions,
            bars_by_day=bars,
            strict_pit=True)


def test_future_bars_do_not_change_an_earlier_snapshot():
    sessions, bars, membership = _world()
    before = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars)

    future_bars = dict(bars)
    future_bars["2026-02-02"] = {
        code: {"close": 999.0, "change_pct": 99.0}
        for code in bars["2026-01-30"]
    }
    after = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=future_bars)

    assert [x.input_hash for x in before] == [x.input_hash for x in after]


def test_a_single_leader_is_visible_in_head_dependence():
    sessions, bars, membership = _world()
    bars["2026-01-30"]["600001"]["close"] = 100.0
    snapshots = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars)
    ai = next(row for row in snapshots if row.sector_id == "AI")
    assert ai.returns_pct["5d"] > ai.ex_top1_5d_median_pct


def test_missing_fund_flow_is_not_zero_fund_flow():
    sessions, bars, membership = _world()
    none = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars)
    assert none[0].fund_flow["available"] is False
    assert none[0].fund_flow["net_amount_sum"] is None

    with_zero = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars,
        fund_flow_by_code={
            code: {"net_amount": 0.0, "net_amount_rate": 0.0}
            for code in bars["2026-01-30"]
        })
    assert with_zero[0].fund_flow["available"] is True
    assert with_zero[0].fund_flow["net_amount_sum"] == 0.0


def test_candidate_leave_one_out_cannot_be_improved_by_its_own_price():
    sessions, bars, membership = _world()
    kwargs = dict(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars,
        market_codes=set(bars["2026-01-30"]),
    )
    before = S.candidate_leave_one_out_5d(**kwargs)
    original = before["AI"]["600001"]

    bars["2026-01-30"]["600001"]["close"] = 1000.0
    after = S.candidate_leave_one_out_5d(**kwargs)["AI"]["600001"]

    assert after == original
    assert after["peer_covered"] == 3
    assert after["peer_total"] == 3


def test_ranking_is_stable():
    sessions, bars, membership = _world()
    snapshots = S.build_sector_snapshots(
        membership=membership,
        decision_at="2026-01-30 09:00:00",
        as_of_session="2026-01-30",
        sessions=sessions,
        bars_by_day=bars)
    ranked = S.rank_sector_snapshots(snapshots)
    assert [row["rank"] for row in ranked] == [1, 2]


def test_direction_journal_preserves_observable_states(store):
    snapshots = [
        {"sector_id": "AI", "rank": 1, "snapshot_hash": "a"},
        {"sector_id": "存储", "rank": 2, "snapshot_hash": "b"},
        {"sector_id": "未知", "rank": None,
         "reason": "missing_required_fact", "snapshot_hash": "c"},
        {"sector_id": "医药", "rank": 4, "snapshot_hash": "d"},
    ]
    set_id = J.record(
        run_id="r1", trader_id="t1", day="2026-01-30", phase="open",
        information_cutoff="2026-01-30 09:00:00",
        architecture="sector_first_v0",
        snapshots=snapshots,
        shortlist=["AI", "存储"],
        selected=["AI"],
        research={"deep_dive_themes": ["AI", "存储"]},
        refusals=[{"sector_id": "存储", "reason": "evidence weak"}],
        conn=store)

    got = {row["sector_id"]: row for row in J.items(set_id, store)}
    assert got["AI"]["status"] == "agent_selected"
    assert got["存储"]["status"] == "agent_rejected"
    assert got["未知"]["status"] == "unassessable"
    assert got["医药"]["status"] == "evaluated_not_offered"


def test_direction_journal_is_append_only(store):
    set_id = J.record(
        run_id="r1", trader_id="t1", day="2026-01-30", phase="open",
        information_cutoff="2026-01-30 09:00:00",
        architecture="sector_first_v0",
        snapshots=[{"sector_id": "AI", "rank": 1, "snapshot_hash": "a"}],
        shortlist=["AI"], selected=["AI"], conn=store)
    with pytest.raises(sqlite3.DatabaseError):
        store.execute(
            "UPDATE theme_opportunity_sets SET architecture='x' WHERE id=?",
            (set_id,))



def test_direction_journal_distinguishes_unreadable_decision(store):
    set_id = J.record(
        run_id="r2", trader_id="t1", day="2026-01-30", phase="open",
        information_cutoff="2026-01-30 09:00:00",
        architecture="sector_first_v0",
        snapshots=[
            {"sector_id": "AI", "rank": 1, "snapshot_hash": "a"},
            {"sector_id": "未知", "rank": None,
             "reason": "missing_required_fact", "snapshot_hash": "b"},
        ],
        shortlist=["AI"], selected=[],
        parse_error="JSONDecodeError: bad reply", conn=store)
    got = {row["sector_id"]: row for row in J.items(set_id, store)}
    assert got["AI"]["status"] == "unreadable_decision"
    assert got["未知"]["status"] == "unassessable"
