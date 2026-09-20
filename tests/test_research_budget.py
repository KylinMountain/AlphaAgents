"""A T1 decision has a finite research funnel, not only a turn ceiling."""

import json

from alpha_agents.tools import budget as B


def _claim(budget, tool, *args, **kwargs):
    return budget.claim(tool, args, kwargs)


class TestResearchBudget:
    def test_total_budget_is_hard(self):
        b = B.ResearchBudget(
            max_total_calls=2, max_market_calls=2, max_theme_calls=2,
            max_stock_calls=2, max_self_calls=2)
        assert _claim(b, "get_market_regime")[0]
        assert _claim(b, "get_my_state")[0]
        ok, reason = _claim(b, "get_theme_state", "AI")
        assert not ok and "总研究预算 2 次" in reason
        assert b.summary()["used"] == 2
        assert b.summary()["denied"] == 1

    def test_category_budget_stops_broad_research(self):
        b = B.ResearchBudget(max_market_calls=1)
        assert _claim(b, "get_market_regime")[0]
        ok, reason = _claim(b, "get_market_regime")
        assert not ok and "market 类预算 1 次" in reason

    def test_only_four_names_may_be_deep_dived(self):
        b = B.ResearchBudget(max_deep_dive_names=2)
        assert _claim(b, "get_stock_context", "600001")[0]
        assert _claim(b, "get_stock_context", "600002")[0]
        ok, reason = _claim(b, "get_stock_context", "600003")
        assert not ok and "最多深挖 2 只股票" in reason
        assert b.summary()["deep_dive_names"] == ["600001", "600002"]

    def test_one_name_cannot_consume_the_whole_budget(self):
        b = B.ResearchBudget(max_calls_per_name=2)
        assert _claim(b, "get_stock_context", "600001")[0]
        assert _claim(b, "get_intraday_shape", code="600001")[0]
        ok, reason = _claim(b, "get_stock_memory", "600001")
        assert not ok and "单票 2 次查询上限" in reason

    def test_denied_call_never_reaches_the_wrapped_function(self):
        called = []

        @B.with_timeout
        def get_market_regime():
            called.append(True)
            return "ok"

        b = B.ResearchBudget(max_market_calls=0)
        with B.use_research_budget(b):
            raw = get_market_regime()
        payload = json.loads(raw)
        assert payload["error"] == "research_budget_exhausted"
        assert called == []

    def test_budget_is_opt_in_for_existing_tool_users(self):
        called = []

        @B.with_timeout
        def get_market_regime():
            called.append(True)
            return "ok"

        assert get_market_regime() == "ok"
        assert called == [True]

    def test_context_does_not_leak_into_the_next_decision(self):
        b = B.ResearchBudget()
        assert B.current_research_budget() is None
        with B.use_research_budget(b):
            assert B.current_research_budget() is b
        assert B.current_research_budget() is None
