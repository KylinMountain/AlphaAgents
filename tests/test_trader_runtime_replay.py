"""T8: historical replay advances the same continuous Trader Runtime."""
from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.data import memory_store
from alpha_agents.evolution import trader_replay as R
from alpha_agents.trader import (
    Action, CompareOp, Condition, DecisionHorizon, EvidenceScope,
    Timeframe, TraderDecision, WatchStatus,
)

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def panel(price=31.0):
    return [{
        "code": "600001", "name": "甲", "close": price,
        "change_pct": 1.0, "adv20": 100000.0,
        "turnover_rate": 2.0, "concepts": ["算力"],
    }]


def wait_decision(dctx):
    return TraderDecision(
        decision_id="wait-1",
        made_at=dctx.information_cutoff,
        action=Action.WAIT,
        code="600001",
        thesis_id=None,
        confidence=.6,
        reasoning="逻辑成立，等待回踩",
        timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.REPLAY_DAILY,
        next_check=(
            Condition(
                "price", CompareOp.LE, 30.0, subject="600001"),),
    )


def test_open_and_close_have_explicit_replay_cutoffs():
    open_ctx = R.context("2026-09-25", "open")
    close_ctx = R.context("2026-09-25", "close")
    assert open_ctx.information_cutoff == datetime(
        2026, 9, 25, 9, 0, tzinfo=TZ)
    assert close_ctx.information_cutoff == datetime(
        2026, 9, 25, 14, 55, tzinfo=TZ)
    assert open_ctx.evidence_scope == EvidenceScope.REPLAY_DAILY


def test_daily_wait_survives_and_wakes_on_next_session_mark(isolated):
    state, dctx, recheck = asyncio.run(R.prepare(
        run_id="replay-1", trader_id="default",
        day="2026-09-25", phase="open",
        panel=panel(31.0),
        market={"regime": "range"},
        marks={"600001": {"price": 31.0, "change_pct": 1.0}},
    ))
    assert recheck == ()
    state = asyncio.run(R.commit(
        state, [wait_decision(dctx)],
        run_id="replay-1", context=dctx))
    assert state.watchlist[0].status == WatchStatus.WATCHING

    next_state, next_ctx, recheck = asyncio.run(R.prepare(
        run_id="replay-1", trader_id="default",
        day="2026-09-28", phase="open",
        panel=[],
        market={"regime": "range"},
        marks={"600001": {"price": 29.8, "change_pct": -0.5}},
    ))
    assert recheck == ("600001",)
    assert next_state.watchlist[0].status == WatchStatus.TRIGGERED
    assert next_ctx.information_cutoff.date().isoformat() == "2026-09-28"


def test_runs_with_same_initial_fact_do_not_share_state(isolated):
    a, _, _ = asyncio.run(R.prepare(
        run_id="a", trader_id="default", day="2026-09-25", phase="open",
        panel=panel(), marks={"600001": {"price": 31.0}}))
    b, _, _ = asyncio.run(R.prepare(
        run_id="b", trader_id="default", day="2026-09-25", phase="open",
        panel=panel(), marks={"600001": {"price": 31.0}}))
    assert a.state_hash == b.state_hash

    _, ctx_a, _ = asyncio.run(R.prepare(
        run_id="a", trader_id="default", day="2026-09-25", phase="close",
        panel=panel(), marks={"600001": {"price": 31.0}}))
    hold = TraderDecision(
        decision_id="a-hold", made_at=ctx_a.information_cutoff,
        action=Action.HOLD, code=None, thesis_id=None, confidence=.5,
        reasoning="不追高", timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.REPLAY_DAILY)
    changed = asyncio.run(R.commit(
        asyncio.run(R.prepare(
            run_id="a", trader_id="default", day="2026-09-25",
            phase="close", panel=panel(),
            marks={"600001": {"price": 31.0}}))[0],
        [hold], run_id="a", context=ctx_a))
    assert changed.state_hash != b.state_hash


def test_walk_forward_prepares_state_before_provider_and_commits_before_execution():
    from scripts import walk_forward as WF

    source = inspect.getsource(WF._decide_llm)
    assert source.index("_trader_runtime_prepare(") < source.index(
        "t1_decider.propose_sync(")
    assert source.index("_trader_runtime_commit(") < source.index(
        "_validate_sector_order_relations(")
    assert source.index("_trader_runtime_commit(") < source.index(
        "capacity_shares(")


def test_walk_forward_open_marks_use_previous_session_not_today():
    from scripts import walk_forward as WF

    source = inspect.getsource(WF._trader_runtime_prepare)
    assert 'mark_day = day if phase == "close" else prev_day' in source


def test_walk_forward_passes_same_runtime_cutoff_to_shared_planner():
    from scripts import walk_forward as WF

    source = inspect.getsource(WF._decide_llm)
    assert "information_cutoff=runtime_context.information_cutoff.isoformat" in source


def test_triggered_watch_is_rebuilt_from_sealed_candidate_evidence(isolated):
    # The adapter stores enough candidate evidence for walk_forward to rebuild
    # a code later even if the fresh selector did not offer it.
    state, dctx, _ = asyncio.run(R.prepare(
        run_id="r", trader_id="default", day="2026-09-25", phase="open",
        panel=[{
            **panel()[0],
            "primary_theme": "算力",
            "membership_snapshot_id": "m1",
            "membership_hash": "h1",
        }],
        marks={"600001": {"price": 31.0}},
    ))
    state = asyncio.run(R.commit(
        state, [wait_decision(dctx)], run_id="r", context=dctx))
    state, _, recheck = asyncio.run(R.prepare(
        run_id="r", trader_id="default", day="2026-09-28", phase="open",
        panel=[], marks={"600001": {"price": 29.0}},
    ))
    assert recheck == ("600001",)
    candidate = next(
        item for item in reversed(state.recent_observations)
        if item.type.value == "candidate")
    assert candidate.data["primary_theme"] == "算力"
    assert candidate.data["membership_snapshot_id"] == "m1"
