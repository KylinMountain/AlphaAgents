"""Sell-side agent exits are a switch; close-time buys no longer exist.

The close buy ran only on the change/turnover control panel. With
Sector-First as the only selection path, and that path 09:00-only, a close
buy is unreachable, so the flag was removed (2026-09-26) rather than left
declared and dead.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402


def _parse(*extra):
    return wf.build_parser().parse_args(
        ["--start", "2026-01-05", *extra])


def test_agent_exits_is_its_own_switch():
    assert _parse("--agent-exits").agent_exits is True
    assert _parse().agent_exits is False


def test_close_buys_is_refused_rather_than_ignored():
    with pytest.raises(SystemExit):
        _parse("--close-buys")


def test_the_buy_decision_is_09_00_only():
    ctx = type("Ctx", (), {"selection_architecture": "sector_first_v0"})()
    with pytest.raises(RuntimeError, match="09:00"):
        wf._decide_llm(ctx, "2026-01-06", "2026-01-05", phase="close")
