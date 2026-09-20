"""Targeted reproductions from AlphaAgents main@eab3f1ec.

No model, network, production database or repository mutation is used.
The propose() and arithmetic functions below were transcribed from the
GitHub connector reads; external SDK/rendering dependencies are stubbed.
This is not the repository's full test suite.
"""
from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace

DEFAULT_MAX_TURNS = 10
DECIDER_NAME = 'sector_selector_v0'
SYSTEM_INSTRUCTIONS = 'stubbed'

def build_message(**kwargs):
    return 'fixture'

def Agent(**kwargs):
    return kwargs

class Runner:
    calls = 0
    @classmethod
    async def run(cls, *args, **kwargs):
        cls.calls += 1
        return SimpleNamespace(final_output='{"themes": []}')

class MaxTurnsExceeded(Exception):
    pass

def parse_selection(raw, offered):
    return {'themes': [], 'refused': [], 'parse_error': None}

# Exact function body: alpha_agents/agents/sector_selector.py:143-186.
async def propose(*, day: str, as_of_session: str, sectors: list[dict],
                  market: dict | None = None, news: list[dict] | None = None,
                  model=None, template: str | None = None, tools: list | None = None,
                  research_budget=None, max_turns: int | None = None) -> dict:
    if model is None:
        from alpha_agents.model_factory import create_model
        model = create_model()
    max_turns = max_turns or DEFAULT_MAX_TURNS
    message = build_message(
        day=day, as_of_session=as_of_session, sectors=sectors,
        market=market, news=news, template=template)

    budget = research_budget
    if tools:
        from alpha_agents.tools.budget import ResearchBudget, use_research_budget
        budget = budget or ResearchBudget()
        message += "\n\n" + budget.prompt_hint()

    agent = Agent(
        name=f"sector_selector:{DECIDER_NAME}",
        instructions=SYSTEM_INSTRUCTIONS,
        model=model,
        tools=list(tools) if tools else [],
    )
    try:
        if budget is None:
            result = await Runner.run(agent, message, max_turns=max_turns)
        else:
            with use_research_budget(budget):
                result = await Runner.run(agent, message, max_turns=max_turns)
    except MaxTurnsExceeded as exc:
        return {
            "themes": [], "refused": [], "raw": "",
            "parse_error": f"MaxTurnsExceeded after {max_turns} turns ({exc})",
            "research_budget": budget.summary() if budget else None,
        }

    raw = result.final_output or ""
    parsed = parse_selection(
        raw, {str(row.get("sector_id") or "") for row in sectors})
    parsed["raw"] = raw
    parsed["research_budget"] = budget.summary() if budget else None
    return parsed

# Exact arithmetic: alpha_agents/evolution/sector_experiment_compare.py.
def _float(value) -> float:
    if value in {None, ""}:
        return 0.0
    return float(value)

def _max_drawdown(values: list[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return abs(worst) * 100.0

def _daily_returns(equity: list[dict]) -> dict[str, float]:
    out = {}
    prior = None
    for row in equity:
        value = _float(row.get("equity"))
        day = str(row.get("date") or "")
        if prior is not None and prior > 0 and day:
            out[day] = (value / prior - 1.0) * 100.0
        prior = value
    return out

async def main():
    result = {}
    try:
        await propose(day='2026-01-30', as_of_session='2026-01-29',
                      sectors=[{'sector_id': 'fixture'}], model=object(),
                      tools=[], research_budget=object())
    except UnboundLocalError as exc:
        result['selector_budget_bug'] = {
            'exception': type(exc).__name__, 'message': str(exc),
            'model_calls_before_failure': Runner.calls,
        }
    else:
        raise AssertionError('Expected the budget bug to reproduce')

    control = await propose(day='2026-01-30', as_of_session='2026-01-29',
                            sectors=[], model=object(), tools=[], research_budget=None)
    assert control['parse_error'] is None
    result['no_budget_control'] = 'passed'

    initial_capital = 100.0
    end_of_day_equity = [95.0, 100.0]
    result['initial_equity_omission'] = {
        'initial_capital': initial_capital,
        'daily_equity': end_of_day_equity,
        'reported_net_return_pct': (end_of_day_equity[-1] / end_of_day_equity[0] - 1) * 100,
        'correct_net_return_pct': (end_of_day_equity[-1] / initial_capital - 1) * 100,
        'reported_drawdown_pct': _max_drawdown(end_of_day_equity),
        'correct_drawdown_pct': _max_drawdown([initial_capital, *end_of_day_equity]),
    }
    duplicate_rows = [
        {'date': '2026-01-05', 'equity': 100 + i}
        for i in range(60)
    ]
    result['duplicate_dates_accepted'] = {
        'input_rows': len(duplicate_rows),
        'daily_return_count': len(_daily_returns(duplicate_rows)),
        'error_raised': False,
    }
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
