from alpha_agents.tools.registry import STOCK_TOOLS


def test_stock_tools_registry_is_populated():
    assert STOCK_TOOLS
    assert all(tool is not None for tool in STOCK_TOOLS)
