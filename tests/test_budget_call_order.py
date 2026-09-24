"""Parallel tool calls take the research budget in the model's order.

2026-09-24: a replay of a recorded run diverged at call #6 because two
parallel calls raced for the last deep-dive slot on threads, and the
``denied`` count echoed to the model came out 2 one time and 1 the next.
"""

from __future__ import annotations

import asyncio
import json
import time

from alpha_agents.tools.budget import (
    ResearchBudget, _async_with_timeout, use_research_budget)


def get_stock_context(code: str, as_of: str = "") -> str:
    # The earlier call is the slower read: finish order is the reverse of
    # call order, which is what made the thread version race.
    time.sleep(0.05 if code == "600001" else 0.0)
    return json.dumps({"available": True, "code": code})


def test_the_first_call_gets_the_slot_every_time():
    tool = _async_with_timeout(get_stock_context)

    async def turn():
        budget = ResearchBudget(max_deep_dive_names=1)
        with use_research_budget(budget):
            got = await asyncio.gather(tool(code="600001"), tool(code="600002"))
        return [json.loads(g).get("available") for g in got], budget.summary()["denied"]

    for _ in range(20):
        assert asyncio.run(turn()) == ([True, False], 1)


def test_a_no_data_answer_is_refunded():
    def get_theme_state(theme: str = "", as_of: str = "") -> str:
        return json.dumps({"available": False, "reason": "no rows"})
    tool = _async_with_timeout(get_theme_state)

    async def go():
        budget = ResearchBudget()
        with use_research_budget(budget):
            await tool(theme="x")
        return budget.refunded
    assert asyncio.run(go()) == 1
