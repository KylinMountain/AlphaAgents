"""T5: position strategy actions live in the continuous Trader Runtime."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.pipeline.tasks import exit_decision as E
from alpha_agents.trader import (
    Action, DecisionContext, DecisionHorizon, EvidenceScope, Observation,
    ObservationType, Session, Timeframe, TraderRuntime, TraderState,
)

TZ = ZoneInfo("Asia/Shanghai")


def at(hour=10, minute=40):
    return datetime(2026, 9, 25, hour, minute, tzinfo=TZ)


def ctx():
    return DecisionContext(
        mode="live",
        observation_resolution=Timeframe.MINUTE_5,
        decision_horizon=DecisionHorizon.POSITION,
        session=Session.INTRADAY,
        information_cutoff=at(),
        evidence_scope=EvidenceScope.LIVE_INTRADAY,
    )


def run(state, observations):
    return asyncio.run(TraderRuntime().step(state, observations, ctx()))


def position_obs(*, shares=1000, avg=10.0):
    return Observation.create(
        observed_at=at(), available_at=at(),
        type=ObservationType.POSITION_CHANGED,
        subjects=["600001"],
        data={
            "code": "600001", "shares": shares, "avg_price": avg,
            "thesis_id": "7",
        },
        source="portfolio_book",
        evidence_refs=[f"position:1:{shares}:{avg}"],
        timeframe=Timeframe.MINUTE_5,
    )


def test_position_snapshot_enters_trader_state():
    state = TraderState.create(trader_id="default", as_of=at())
    result = run(state, [position_obs()])
    assert result.state.positions[0].code == "600001"
    assert result.state.positions[0].shares == 1000
    assert result.state.positions[0].avg_price == 10.0
    assert result.state.positions[0].thesis_id == "7"
    assert result.reevaluate_subjects == ("600001",)


def test_position_size_change_and_close_are_explicit_transitions():
    state = TraderState.create(trader_id="default", as_of=at())
    opened = run(state, [position_obs()]).state

    later_ctx = DecisionContext(
        mode="live", observation_resolution=Timeframe.MINUTE_5,
        decision_horizon=DecisionHorizon.POSITION,
        session=Session.INTRADAY,
        information_cutoff=at(10, 45),
        evidence_scope=EvidenceScope.LIVE_INTRADAY)
    resized = asyncio.run(TraderRuntime().step(
        opened,
        [Observation.create(
            observed_at=at(10, 45), available_at=at(10, 45),
            type=ObservationType.POSITION_CHANGED, subjects=["600001"],
            data={"code":"600001","shares":1500,"avg_price":10.2,"thesis_id":"7"},
            source="portfolio_book", evidence_refs=["position:1:1500:10.2"],
            timeframe=Timeframe.MINUTE_5)],
        later_ctx))
    assert resized.state.positions[0].shares == 1500
    assert resized.transitions[-1].reason == "position size/cost changed"

    close_ctx = DecisionContext(
        mode="live", observation_resolution=Timeframe.MINUTE_5,
        decision_horizon=DecisionHorizon.POSITION,
        session=Session.INTRADAY,
        information_cutoff=at(10, 50),
        evidence_scope=EvidenceScope.LIVE_INTRADAY)
    closed = asyncio.run(TraderRuntime().step(
        resized.state,
        [Observation.create(
            observed_at=at(10, 50), available_at=at(10, 50),
            type=ObservationType.POSITION_CHANGED, subjects=["600001"],
            data={"code":"600001","shares":0,"avg_price":10.2,"thesis_id":"7"},
            source="portfolio_book", evidence_refs=["position-closed:600001"],
            timeframe=Timeframe.MINUTE_5)],
        close_ctx))
    assert closed.state.positions == ()
    assert closed.transitions[-1].to_status == "closed"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"code":"600001","action":"hold","reason":"","confidence":"low"}, Action.HOLD),
        ({"code":"600001","action":"add","reason":"趋势强化","size_pct":0.05}, Action.ADD),
        ({"code":"600001","action":"trim","reason":"降低风险","fraction":0.4}, Action.REDUCE),
        ({"code":"600001","action":"sell","reason":"论点失效"}, Action.SELL),
    ],
)
def test_exit_actions_map_into_one_trader_vocabulary(raw, expected):
    [decision] = E.to_runtime_decisions(
        [raw], ctx(), decision_key="exit-1")
    assert decision.action == expected
    assert decision.code == "600001"


def test_reduce_fraction_and_add_size_are_preserved():
    reduce = E.to_runtime_decisions(
        [{"code":"600001","action":"trim","reason":"减仓","fraction":0.35}],
        ctx(), decision_key="exit-r")[0]
    add = E.to_runtime_decisions(
        [{"code":"600001","action":"add","reason":"加仓","size_pct":1.2}],
        ctx(), decision_key="exit-a")[0]
    assert reduce.fraction == 0.35
    assert add.size_pct == 1.2


def test_runtime_commit_happens_before_discretionary_execution(monkeypatch):
    positions = [{"id":1,"code":"600001","shares":1000,"open_price":10.0,
                  "thesis_id":7,"name":"甲"}]
    prices = {"600001": 11.0}
    order = []

    monkeypatch.setattr(E, "get_open_positions", lambda trader_id=None: positions)
    monkeypatch.setattr(
        "alpha_agents.data.thesis.get_active",
        lambda **kwargs: None)

    async def decide(context, trader=None, **kwargs):
        return [{"code":"600001","action":"sell","reason":"逻辑失效",
                 "confidence":"high"}]

    async def seal(decisions, pos, **kwargs):
        order.append("seal")
        return ()

    def apply(decisions, pos, price_map):
        order.append("apply")
        return [{"type":"agent_exit","code":"600001"}]

    monkeypatch.setattr(E, "decide", decide)
    monkeypatch.setattr(E, "commit_runtime_decisions", seal)
    monkeypatch.setattr(E, "apply", apply)
    got = asyncio.run(E.run(
        prices, [{"type":"signal","code":"600001"}],
        trader_id="default"))
    assert got[0]["type"] == "agent_exit"
    assert order == ["seal", "apply"]


def test_state_write_failure_holds_instead_of_executing(monkeypatch):
    positions = [{"id":1,"code":"600001","shares":1000,"open_price":10.0,
                  "thesis_id":7,"name":"甲"}]
    monkeypatch.setattr(E, "get_open_positions", lambda trader_id=None: positions)
    monkeypatch.setattr(
        "alpha_agents.data.thesis.get_active",
        lambda **kwargs: None)

    async def decide(context, trader=None, **kwargs):
        return [{"code":"600001","action":"sell","reason":"逻辑失效"}]

    async def broken(*args, **kwargs):
        raise RuntimeError("state store down")

    monkeypatch.setattr(E, "decide", decide)
    monkeypatch.setattr(E, "commit_runtime_decisions", broken)
    monkeypatch.setattr(
        E, "apply",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("unsealed discretionary decision must not execute")))
    assert asyncio.run(E.run(
        {"600001":11.0}, [{"type":"signal","code":"600001"}],
        trader_id="default")) == []
