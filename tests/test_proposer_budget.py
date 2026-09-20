"""RP-01: real proposers, renderers, parsers and tool-budget contexts.

Only the SDK Runner's external model loop is replaced. Agent construction and
FunctionTool invocation remain real; no live model or market data is needed.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import RunContextWrapper, function_tool
from agents.exceptions import MaxTurnsExceeded

from alpha_agents.agents import sector_selector, sector_stock_selector, t1_decider
from alpha_agents.tools import budget as B


_MODULES = (sector_selector, sector_stock_selector, t1_decider)
_PANEL = [{"code": "600001", "name": "fixture", "close": 10.0,
           "change_pct": 2.0, "adv20": 100000}]
_REPLY = json.dumps({
    "themes": [{"sector_id": "AI", "thesis": "fixture", "invalidations": []}],
    "stocks": [{"code": "600001", "reason": "fixture"}],
    "orders": [{"code": "600001", "entry_low": 9.8, "entry_high": 10.2,
                "stop_loss": 9.0, "target_price": 12.0, "reason": "fixture"}],
})


@pytest.fixture(params=_MODULES, ids=lambda module: module.__name__.rsplit(".", 1)[-1])
def proposer(request):
    return request.param


@pytest.fixture
def tool():
    reached = []

    @B.with_timeout
    def get_market_regime() -> str:
        """Return an offline market fixture."""
        reached.append(True)
        return json.dumps({"available": True})

    return function_tool(get_market_regime), reached


async def _call_tool(agent):
    return await agent.tools[0].on_invoke_tool(RunContextWrapper(context=None), "{}")


async def _propose(module, *, tools, budget):
    common = {
        "day": "2026-01-30", "news": [], "model": "offline-test-model",
        "tools": tools, "research_budget": budget,
    }
    if module is sector_selector:
        return await module.propose(
            as_of_session="2026-01-29", sectors=[{"sector_id": "AI"}], **common)
    return await module.propose(
        prev_day="2026-01-29", panel=_PANEL, market={}, **common)


@pytest.mark.parametrize("has_tools", [False, True], ids=["no-tools", "tools"])
@pytest.mark.parametrize("has_budget", [False, True], ids=["no-budget", "budget"])
def test_real_proposer_tools_budget_matrix(proposer, tool, monkeypatch,
                                          has_tools, has_budget):
    offered, reached = tool
    supplied = B.ResearchBudget(max_total_calls=2) if has_budget else None
    observed = []

    async def run(agent, message, **kwargs):
        active = B.current_research_budget()
        observed.append(active)
        assert bool(agent.tools) is has_tools
        assert agent.model == "offline-test-model"
        if supplied is not None:
            assert active is supplied
        elif has_tools:
            assert isinstance(active, B.ResearchBudget)
        else:
            assert active is None
        await asyncio.sleep(0)
        assert B.current_research_budget() is active
        if has_tools:
            assert active.prompt_hint() in message
            assert json.loads(await _call_tool(agent))["available"] is True
        else:
            assert "【研究预算（系统硬约束）】" not in message
        return SimpleNamespace(final_output=_REPLY)

    monkeypatch.setattr(proposer.Runner, "run", run)

    async def scenario():
        assert B.current_research_budget() is None
        result = await _propose(
            proposer, tools=[offered] if has_tools else [], budget=supplied)
        assert B.current_research_budget() is None
        return result

    got = asyncio.run(scenario())
    assert len(observed) == 1
    assert got["parse_error"] is None
    key = ("themes" if proposer is sector_selector else
           "stocks" if proposer is sector_stock_selector else "orders")
    assert len(got[key]) == 1
    assert len(reached) == int(has_tools)
    active = observed[0]
    if active is None:
        assert got["research_budget"] is None
    else:
        assert got["research_budget"] == active.summary()
        assert active.total_calls == int(has_tools)
        assert active.denied == 0


@pytest.mark.parametrize("has_tools", [False, True], ids=["no-tools", "tools"])
@pytest.mark.parametrize("outcome", ["success", "malformed", "max_turns",
                                     "provider_error", "cancelled"])
def test_real_proposer_restores_outer_context(proposer, tool, monkeypatch,
                                             has_tools, outcome):
    offered, _ = tool
    outer = B.ResearchBudget()
    supplied = B.ResearchBudget(max_total_calls=1)
    calls = []

    async def run(agent, message, **kwargs):
        calls.append(True)
        assert B.current_research_budget() is supplied
        await asyncio.sleep(0)
        if outcome == "max_turns":
            raise MaxTurnsExceeded("offline fixture")
        if outcome == "provider_error":
            raise RuntimeError("offline provider failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return SimpleNamespace(final_output=(
            "not-json" if outcome == "malformed" else _REPLY))

    monkeypatch.setattr(proposer.Runner, "run", run)

    async def scenario():
        with B.use_research_budget(outer):
            try:
                if outcome in {"provider_error", "cancelled"}:
                    expected = (RuntimeError if outcome == "provider_error"
                                else asyncio.CancelledError)
                    with pytest.raises(expected):
                        await _propose(
                            proposer, tools=[offered] if has_tools else [],
                            budget=supplied)
                else:
                    got = await _propose(
                        proposer, tools=[offered] if has_tools else [], budget=supplied)
                    if outcome == "success":
                        assert got["parse_error"] is None
                    else:
                        assert got["parse_error"]
                    assert got["research_budget"] == supplied.summary()
            finally:
                # Check inside the same task; asyncio.run() isolation alone
                # would hide a leaked ContextVar from the next decision.
                assert B.current_research_budget() is outer
        assert B.current_research_budget() is None

    asyncio.run(scenario())
    assert calls == [True]
    assert outer.total_calls == supplied.total_calls == 0


def test_direction_stock_planner_share_budget_without_reset(tool, monkeypatch):
    offered, reached = tool
    supplied = B.ResearchBudget(max_total_calls=1)
    observed = []

    async def run(agent, message, **kwargs):
        assert B.current_research_budget() is supplied
        observed.append(agent.name)
        if agent.tools:
            assert json.loads(await _call_tool(agent))["available"] is True
            denied = json.loads(await _call_tool(agent))
            assert denied["error"] == "research_budget_exhausted"
        return SimpleNamespace(final_output=_REPLY)

    monkeypatch.setattr(sector_selector.Runner, "run", run)

    async def scenario():
        direction = await _propose(sector_selector, tools=[], budget=supplied)
        stock = await _propose(sector_stock_selector, tools=[offered], budget=supplied)
        planner = await _propose(t1_decider, tools=[], budget=supplied)
        assert B.current_research_budget() is None
        return direction, stock, planner

    direction, stock, planner = asyncio.run(scenario())
    assert len(observed) == 3
    assert reached == [True]
    assert direction["research_budget"]["used"] == 0
    assert stock["research_budget"]["used"] == 1
    assert planner["research_budget"]["used"] == 1
    assert planner["research_budget"]["denied"] == 1


def test_exhausted_budget_cannot_be_reset_by_proposer_retry(proposer, tool, monkeypatch):
    offered, reached = tool
    supplied = B.ResearchBudget(max_total_calls=0)
    calls = []

    async def run(agent, message, **kwargs):
        calls.append(True)
        assert B.current_research_budget() is supplied
        result = json.loads(await _call_tool(agent))
        assert result["error"] == "research_budget_exhausted"
        return SimpleNamespace(final_output=_REPLY)

    monkeypatch.setattr(proposer.Runner, "run", run)

    async def scenario():
        for _ in range(2):
            await _propose(proposer, tools=[offered], budget=supplied)
            assert B.current_research_budget() is None

    asyncio.run(scenario())
    assert calls == [True, True]
    assert reached == []
    assert supplied.total_calls == 0
    assert supplied.denied == 2
