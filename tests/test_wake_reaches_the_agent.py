"""A wake-up that arrives nowhere is worse than no wake-up.

``exit_decision.run`` decides which positions are worth a model call, and
it excluded any position that had an active thesis:

    elif code in signal_codes and not get_active(code=code, ...)

That was right while ``thesis_monitor`` closed those positions in code --
asking the model about a position a rule had already settled would have
been 48 calls a day for nothing. With the invalidation now producing a
signal instead of a close, the same line is exactly backwards: the one
position that must reach the model is the one whose stated invalidation
just fired, and it is the one this filter dropped.

The filter still earns its keep for a bare rule signal on a position with
a plan on file, so the distinction is the ``thesis_id`` the wake carries.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from alpha_agents.pipeline.tasks import exit_decision as ED


def _pos(code):
    return {"code": code, "name": "测试", "shares": 100, "open_price": 10.0,
            "theme": "AI算力", "holding_days": 3, "peak_return_pct": 15.0,
            "reason": "买入理由"}


def _wake(code, thesis_id=7):
    return {"type": "signal", "code": code, "thesis_id": thesis_id,
            "kind": "drawdown_from_peak",
            "reason": "你声明的失效条件触发：从峰值回撤超过 8%。还持有吗？"}


class TestAWokenPositionReachesTheModel:
    @pytest.fixture
    def asked(self):
        """Returns the positions run() actually put in front of the model."""
        seen = {}

        def _capture(positions, price_map, signals, **kw):
            seen["codes"] = [p["code"] for p in positions]
            seen["context"] = "ctx"
            return "ctx"

        with patch.object(ED, "build_context", side_effect=_capture), \
             patch.object(ED, "decide", new=AsyncMock(return_value=[])):
            yield seen

    async def _run(self, signals, positions, active_codes):
        def _get_active(code=None, trader_id=None):
            return [object()] if code in active_codes else []

        with patch.object(ED, "get_open_positions", return_value=positions), \
             patch("alpha_agents.data.thesis.get_active", _get_active):
            return await ED.run({p["code"]: 11.0 for p in positions}, signals)

    @pytest.mark.asyncio
    async def test_a_thesis_with_a_fired_invalidation_is_asked(self, asked):
        """The case the old filter dropped."""
        await self._run([_wake("300308")], [_pos("300308")],
                        active_codes={"300308"})
        assert asked["codes"] == ["300308"]

    @pytest.mark.asyncio
    async def test_a_bare_rule_signal_on_a_planned_position_is_still_skipped(
            self, asked):
        """The cost control the filter exists for, unchanged: there is a
        plan on file and no commitment has come due."""
        bare = {"type": "signal", "code": "300308", "reason": "跌破均线"}
        await self._run([bare], [_pos("300308")], active_codes={"300308"})
        assert asked.get("codes") is None, "no model call was needed"

    @pytest.mark.asyncio
    async def test_a_rule_signal_with_no_plan_is_asked_as_before(self, asked):
        bare = {"type": "signal", "code": "300308", "reason": "跌破均线"}
        await self._run([bare], [_pos("300308")], active_codes=set())
        assert asked["codes"] == ["300308"]

    @pytest.mark.asyncio
    async def test_an_unrelated_position_is_left_alone(self, asked):
        await self._run([_wake("300308")],
                        [_pos("300308"), _pos("600519")],
                        active_codes={"300308", "600519"})
        assert asked["codes"] == ["300308"]


class TestTheLiveLoopForwardsThem:
    def test_book_manager_passes_signals_on(self):
        from alpha_agents.pipeline.tasks import book_manager
        src = inspect.getsource(book_manager)
        assert 'pos_alerts += result["signals"]' in src, (
            "the live path drops the wake-ups")

    def test_the_replay_passes_signals_on(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        assert "signals=ctx.pending_signals" in inspect.getsource(wf._agent_exits)

    def test_the_replay_clears_them_before_every_check(self):
        """Yesterday's wake-up would look exactly like a fresh one."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        src = inspect.getsource(wf._check_theses)
        body = src.split("bars = ctx.corpus.bars")[0]
        assert "ctx.pending_signals = []" in body, (
            "must be cleared before any early return")
