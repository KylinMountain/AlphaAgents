"""A tool the agent calls during a replay answers as of the replay's instant.

2026-09-24: four 30-day replays served September 2026 bars to January 2026
decisions in 7% of get_stock_context calls. The tool ran on a worker thread
that did not carry the replay's contextvar, read "no as-of" as "live", and
answered for today.
"""

from __future__ import annotations

import asyncio

import pytest

from alpha_agents.evolution import replay_mode
from alpha_agents.tools import budget as B


def _probe(code: str = "", as_of: str = "") -> str:
    from alpha_agents.tools.trader_tools import _as_of
    return str(_as_of())


def test_the_agent_tool_thread_sees_the_replay_instant():
    tool = B._async_with_timeout(_probe)

    async def go():
        with replay_mode.replay_as_of("2026-01-05 09:00"):
            return await tool(code="600001")
    assert asyncio.run(go()) == "2026-01-05 09:00"


def test_the_sync_wrapper_thread_sees_it_too():
    with replay_mode.replay_as_of("2026-01-05 09:00"):
        assert B.with_timeout(_probe)(code="600001") == "2026-01-05 09:00"


def test_a_lost_instant_in_a_replay_fails_instead_of_reading_today(monkeypatch):
    from alpha_agents.tools.trader_tools import _as_of
    monkeypatch.setattr(replay_mode, "_REPLAY_PROCESS", True)
    with pytest.raises(RuntimeError):
        _as_of()
    monkeypatch.setattr(replay_mode, "_REPLAY_PROCESS", False)
    assert _as_of() is None, "live is still live"
