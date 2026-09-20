"""No-flow experiment tools cannot reveal ablated fund-flow fields."""

import json

from alpha_agents.tools import trader_tools as T


def test_no_flow_theme_view_removes_sector_flow():
    raw = json.dumps({
        "available": True,
        "theme": "AI",
        "highest_streak": 3,
        "sector_flow": {
            "net_flow_yi": 12.3,
            "company_count": 20,
        },
    })
    got = json.loads(T._without_keys(raw, "sector_flow"))
    assert got["highest_streak"] == 3
    assert "sector_flow" not in got


def test_no_flow_stock_view_removes_fund_flow():
    raw = json.dumps({
        "available": True,
        "code": "600001",
        "atr14": 0.5,
        "fund_flow": {"5d_net": 123},
    })
    got = json.loads(T._without_keys(raw, "fund_flow"))
    assert got["atr14"] == 0.5
    assert "fund_flow" not in got


def test_no_flow_tool_whitelist_never_exposes_original_flow_bearing_tools():
    names = {tool.name for tool in T.NO_FLOW_TRADER_TOOLS}
    assert "get_theme_state" not in names
    assert "get_stock_context" not in names
    assert "get_theme_state_no_flow" in names
    assert "get_stock_context_no_flow" in names
    assert len(names) == len(T.NO_FLOW_TRADER_TOOLS)
