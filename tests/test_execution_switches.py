"""RP-08: sell-side agent exits and close-time buys are independent."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402


def _parse(*extra):
    return wf.build_parser().parse_args(
        ["--start", "2026-01-05", *extra])


def test_agent_exits_does_not_enable_close_buys():
    args = _parse("--agent-exits")
    assert args.agent_exits is True
    assert args.close_buys is False


def test_close_buys_does_not_enable_agent_exits():
    args = _parse("--close-buys")
    assert args.close_buys is True
    assert args.agent_exits is False


def test_both_flags_can_be_enabled_explicitly():
    args = _parse("--agent-exits", "--close-buys")
    assert args.agent_exits is True
    assert args.close_buys is True


@pytest.mark.parametrize(
    "agent_exits,close_buys,expected",
    [
        (False, False, False),
        (True, False, False),
        (False, True, True),
        (True, True, True),
    ],
)
def test_close_buy_routing_depends_only_on_close_buy_switch(
        agent_exits, close_buys, expected):
    ctx = SimpleNamespace(
        decider="llm", agent_exits=agent_exits, close_buys=close_buys)
    assert wf._close_buy_enabled(ctx) is expected


def test_placeholder_never_runs_close_buy_even_when_switch_is_set():
    ctx = SimpleNamespace(
        decider="placeholder", agent_exits=False, close_buys=True)
    assert wf._close_buy_enabled(ctx) is False


@pytest.mark.parametrize(
    "architecture",
    [
        "sector_first_v0",
        "sector_first_simple_selector",
        "sector_first_no_flow",
        "sector_rank_price_v1",
    ],
)
def test_sector_reproducible_paths_refuse_close_buy(architecture):
    with pytest.raises(SystemExit, match="does not support --close-buys"):
        wf._validate_close_buy_support(architecture, True)


def test_supported_open_architecture_accepts_independent_switch():
    wf._validate_close_buy_support("dual_rank_v0", True)
    wf._validate_close_buy_support("dual_rank_price_v1", True)
