"""An empty data source must not spend the agent's research budget.

``ResearchBudget.claim`` reserves before the call, so a tool that answers
"this source has no rows for that session" spent a call anyway. Measured on
the 2026-01-05 replay: the agent put all four of its ``theme`` calls into
``get_theme_state``, whose table (``limit_pool_snapshots``) starts 2026-08-25
and is therefore empty for the whole January window, and then had nothing
left for the theme questions it could have answered.

The budget prices research. It should not price this repository's data gaps.
"""

import json

import pytest

from alpha_agents.tools.budget import (
    ResearchBudget, use_research_budget, with_timeout)


def _no_data(**extra):
    return json.dumps({"available": False, "what": "theme_state",
                       "reason": "窗口内无快照", **extra}, ensure_ascii=False)


def _answer():
    return json.dumps({"available": True, "rows": [1, 2, 3]})


def get_theme_state(theme="x", as_of=""):
    return _no_data()


def get_market_regime(as_of=""):
    return _answer()


def test_a_no_data_answer_costs_nothing():
    budget = ResearchBudget()
    wrapped = with_timeout(get_theme_state)
    with use_research_budget(budget):
        for _ in range(10):
            wrapped(theme="固态电池")
    assert budget.total_calls == 0
    assert budget.refunded == 10
    assert budget.denied == 0, "an empty source must never exhaust a category"


def test_a_real_answer_still_costs_one():
    budget = ResearchBudget()
    wrapped = with_timeout(get_market_regime)
    with use_research_budget(budget):
        wrapped()
    assert budget.total_calls == 1
    assert budget.refunded == 0


def test_the_trace_names_the_empty_answer():
    """"no_data" and "ok" are different facts about what the model received."""
    budget = ResearchBudget()
    with use_research_budget(budget):
        with_timeout(get_theme_state)(theme="固态电池")
        with_timeout(get_market_regime)()
    assert [row["status"] for row in budget.trace()] == ["no_data", "ok"]


def test_an_unparseable_reply_is_not_refunded():
    """A refund is a favour; guessing wrong hands out free calls."""
    def weird(as_of=""):
        return "not json at all"
    budget = ResearchBudget()
    with use_research_budget(budget):
        with_timeout(weird)()
    assert budget.total_calls == 1
    assert budget.refunded == 0


def test_refunding_a_per_stock_call_frees_the_deep_dive_slot():
    """Otherwise an empty stock burns one of the four names for the session."""
    def get_stock_context(code="", as_of=""):
        return _no_data(code=code)

    budget = ResearchBudget()
    wrapped = with_timeout(get_stock_context)
    with use_research_budget(budget):
        for code in ("600001", "600002", "600003", "600004", "600005"):
            wrapped(code=code)
    assert budget.deep_dive_names == set()
    assert budget.denied == 0, "five empty stocks must not use up four slots"


def test_a_timeout_is_still_charged():
    """The opposite case: a hang spends the largest share of the deadline."""
    import time
    from alpha_agents.tools import budget as budget_mod

    def slow(as_of=""):
        time.sleep(0.3)
        return _answer()

    b = ResearchBudget()
    with use_research_budget(b):
        with_timeout(slow, timeout=0)(as_of="2026-01-05")
    assert b.total_calls == 1
    assert b.refunded == 0


def test_a_raising_tool_is_still_charged():
    """It spent real time before failing."""
    def boom(as_of=""):
        raise RuntimeError("upstream 500")

    b = ResearchBudget()
    with use_research_budget(b):
        with_timeout(boom)()
    assert b.total_calls == 1
    assert b.refunded == 0
