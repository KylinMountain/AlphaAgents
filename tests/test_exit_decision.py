"""The sell side, once the agent owns it.

Two properties matter more than the decisions themselves. Every failure
path has to hold rather than sell — a timeout, a malformed block, an
unknown code — because a spurious exit is a realised loss while a missed
one still has the hard stop underneath it. And the hard stop has to stay
out of the agent's reach, or "discretion above the floor" is just
discretion.
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import portfolio
from alpha_agents.pipeline.tasks import exit_decision


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def block(payload: str) -> str:
    return f"判断如下。\n\n<!-- DECISIONS: {payload} -->\n"


class TestEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_toggle_spellings(self, monkeypatch, value):
        monkeypatch.setenv("AGENT_EXIT_DECISIONS", value)
        assert exit_decision.enabled()

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_EXIT_DECISIONS", raising=False)
        assert not exit_decision.enabled()


class TestParsing:
    def test_reads_a_well_formed_block(self):
        out = exit_decision.parse_decisions(block(
            '[{"code":"600835","action":"sell","reason":"主线归档，逻辑已破",'
            '"confidence":"high"}]'))
        assert out == [{"code": "600835", "action": "sell",
                        "reason": "主线归档，逻辑已破", "confidence": "high",
                        "fraction": None, "size_pct": None}]

    def test_no_block_holds_everything(self, caplog):
        with caplog.at_level("WARNING"):
            assert exit_decision.parse_decisions("我觉得都该卖了。") == []
        assert any("holding all" in r.getMessage() for r in caplog.records)

    def test_malformed_json_holds_everything(self, caplog):
        with caplog.at_level("WARNING"):
            assert exit_decision.parse_decisions(
                block('[{"code":"600835",,,}]')) == []
        assert any("malformed" in r.getMessage() for r in caplog.records)

    def test_unknown_action_is_dropped(self):
        assert exit_decision.parse_decisions(block(
            '[{"code":"600835","action":"短线出掉","reason":"x"}]')) == []

    def test_a_sell_with_no_reason_is_refused(self, caplog):
        """An unexplained exit is exactly what this redesign was for."""
        with caplog.at_level("WARNING"):
            out = exit_decision.parse_decisions(block(
                '[{"code":"600835","action":"sell","reason":""}]'))
        assert out == []
        assert any("no reason" in r.getMessage() for r in caplog.records)

    def test_hold_needs_no_reason(self):
        out = exit_decision.parse_decisions(block(
            '[{"code":"600835","action":"hold","reason":""}]'))
        assert len(out) == 1


class TestApply:
    POS = [{"id": 7, "code": "600835", "name": "上海机电", "shares": 800,
            "open_price": 18.40}]

    def test_sell_closes_with_the_agent_reason(self):
        with patch.object(exit_decision, "close_position",
                          return_value=True) as close:
            alerts = exit_decision.apply(
                [{"code": "600835", "action": "sell", "reason": "主线转弱",
                  "confidence": "high"}],
                self.POS, {"600835": 18.56})
        assert close.call_args.kwargs["close_reason"] == "agent卖出: 主线转弱"
        assert alerts[0]["type"] == "agent_exit"

    def test_hold_touches_nothing(self):
        with patch.object(exit_decision, "close_position") as close:
            alerts = exit_decision.apply(
                [{"code": "600835", "action": "hold", "reason": ""}],
                self.POS, {"600835": 18.56})
        close.assert_not_called()
        assert alerts == []

    def test_trim_is_recorded_as_a_reducing_exit(self):
        with patch.object(exit_decision, "close_position",
                          return_value=True) as close:
            exit_decision.apply(
                [{"code": "600835", "action": "trim", "reason": "回撤一半"}],
                self.POS, {"600835": 18.56})
        assert close.call_args.kwargs["close_reason"].startswith("agent减仓:")

    def test_unknown_code_is_skipped(self, caplog):
        with patch.object(exit_decision, "close_position") as close, \
             caplog.at_level("WARNING"):
            exit_decision.apply(
                [{"code": "000001", "action": "sell", "reason": "x"}],
                self.POS, {"600835": 18.56})
        close.assert_not_called()

    def test_a_position_with_no_price_is_skipped(self):
        with patch.object(exit_decision, "close_position") as close:
            exit_decision.apply(
                [{"code": "600835", "action": "sell", "reason": "x"}],
                self.POS, {})
        close.assert_not_called()


class TestContext:
    def test_names_the_hard_line_and_the_buy_reason(self):
        pos = [{"code": "600835", "name": "上海机电", "shares": 800,
                "open_price": 18.40, "peak_return_pct": 3.2,
                "holding_days": 4, "theme": "国企改革",
                "reason": "低涨幅+流动性好"}]
        with patch.object(exit_decision, "_theme_line",
                          return_value="国企改革 累计强度2/10 今日+2 watching"), \
             patch.object(exit_decision, "_news_for_theme", return_value=[]):
            out = exit_decision.build_context(pos, {"600835": 18.56}, [])

        assert "风控硬线" in out
        assert "买入理由: 低涨幅+流动性好" in out
        assert "18.40" in out and "18.56" in out
        assert "无相关快讯" in out or "无（主线无新消息" in out

    def test_rule_signals_reach_the_agent(self):
        pos = [{"code": "600835", "name": "上海机电", "shares": 800,
                "open_price": 18.40, "theme": "", "reason": ""}]
        signals = [{"type": "signal", "code": "600835", "reason": "移动止损触发"}]
        with patch.object(exit_decision, "_news_for_theme", return_value=[]):
            out = exit_decision.build_context(pos, {"600835": 18.56}, signals)
        assert "规则信号: 移动止损触发" in out


class TestHardFloor:
    def test_a_big_loss_is_hard(self, store):
        assert portfolio._is_hard_exit({"theme": ""}, -8.5)

    def test_a_survivable_loss_is_not(self, store):
        assert not portfolio._is_hard_exit({"theme": ""}, -7.9)

    def test_an_archived_theme_is_hard(self, store):
        from alpha_agents.data.memory_store import upsert_theme
        upsert_theme("旧主线", status="archived", strength=0)
        assert portfolio._is_hard_exit({"theme": "旧主线"}, 2.0)

    def test_a_weak_but_live_theme_is_not(self, store):
        from alpha_agents.data.memory_store import upsert_theme
        upsert_theme("国企改革", status="watching", strength=2)
        assert not portfolio._is_hard_exit({"theme": "国企改革"}, -3.0)

    def test_threshold_is_configurable(self, monkeypatch):
        """A tighter account wants a tighter floor without a code change.

        Patched on position_monitor: _is_hard_exit lives there since the
        split and binds HARD_STOP_PCT at import, so patching the name on
        portfolio no longer reaches it."""
        from alpha_agents.data import position_monitor
        monkeypatch.setattr(position_monitor, "HARD_STOP_PCT", 5.0)
        assert portfolio._is_hard_exit({"theme": ""}, -5.1)
        assert not portfolio._is_hard_exit({"theme": ""}, -4.9)


POS = [{"id": 1, "code": "600835", "name": "上海机电",
        "open_price": 18.4, "theme": "", "reason": ""}]
SIGNAL = [{"type": "signal", "code": "600835", "reason": "移动止损触发"}]


class TestWhoReachesTheModel:
    """Since theses moved the routine checks into code, a model call has to
    earn its place: a narrative condition due for review, or a rule signal
    on a position with no plan on file. A quiet cycle costs nothing."""

    @pytest.mark.asyncio
    async def test_a_quiet_cycle_calls_nothing(self):
        with patch.object(exit_decision, "get_open_positions", return_value=POS), \
             patch("alpha_agents.data.thesis.get_active", return_value=[]), \
             patch.object(exit_decision, "decide") as decide:
            assert await exit_decision.run({"600835": 18.5}, []) == []
        decide.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_signal_on_a_position_with_a_thesis_is_left_to_the_thesis(self):
        """The plan already covers it; asking again is what we removed."""
        with patch.object(exit_decision, "get_open_positions", return_value=POS), \
             patch("alpha_agents.data.thesis.get_active",
                   return_value=["a thesis"]), \
             patch.object(exit_decision, "decide") as decide:
            assert await exit_decision.run({"600835": 18.5}, SIGNAL) == []
        decide.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_signal_with_no_thesis_does_reach_the_model(self):
        with patch.object(exit_decision, "get_open_positions", return_value=POS), \
             patch("alpha_agents.data.thesis.get_active", return_value=[]), \
             patch.object(exit_decision, "_news_for_theme", return_value=[]), \
             patch.object(exit_decision, "decide", return_value=[]) as decide:
            await exit_decision.run({"600835": 18.5}, SIGNAL)
        decide.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_narrative_review_reaches_the_model(self):
        from alpha_agents.data.thesis import Thesis
        due = [(Thesis(code="600835"), None)]
        with patch.object(exit_decision, "get_open_positions", return_value=POS), \
             patch("alpha_agents.data.thesis.get_active", return_value=[]), \
             patch.object(exit_decision, "_news_for_theme", return_value=[]), \
             patch.object(exit_decision, "decide", return_value=[]) as decide:
            await exit_decision.run({"600835": 18.5}, [], narrative_due=due)
        decide.assert_called_once()


class TestRunDegradesToHold:
    @pytest.mark.asyncio
    async def test_no_positions_is_a_no_op(self):
        with patch.object(exit_decision, "get_open_positions", return_value=[]):
            assert await exit_decision.run({}, []) == []

    @pytest.mark.asyncio
    async def test_an_agent_failure_holds(self, caplog):
        with patch.object(exit_decision, "get_open_positions", return_value=POS), \
             patch("alpha_agents.data.thesis.get_active", return_value=[]), \
             patch.object(exit_decision, "_news_for_theme", return_value=[]), \
             patch.object(exit_decision, "decide",
                          side_effect=RuntimeError("model down")), \
             patch.object(exit_decision, "close_position") as close, \
             caplog.at_level("WARNING"):
            assert await exit_decision.run({"600835": 18.5}, SIGNAL) == []
        close.assert_not_called()
        assert any("holding all" in r.getMessage() for r in caplog.records)


class TestSizingIsTheAgentsCall:
    """加仓多少、减仓多少、什么时候加——这些以前是文件里的常数，所以
    学不到。现在是决策，会连同结果一起被复盘。"""

    POS = [{"id": 7, "code": "600835", "name": "上海机电", "shares": 1000,
            "open_price": 18.40}]

    def test_trim_uses_the_stated_fraction(self):
        with patch.object(exit_decision, "close_position",
                          return_value=True) as close:
            exit_decision.apply(
                [{"code": "600835", "action": "trim", "reason": "回吐一半",
                  "fraction": 0.3}], self.POS, {"600835": 18.56})
        assert close.call_args.kwargs["shares"] == 300

    def test_trim_without_a_fraction_takes_half(self):
        with patch.object(exit_decision, "close_position",
                          return_value=True) as close:
            exit_decision.apply(
                [{"code": "600835", "action": "trim", "reason": "x"}],
                self.POS, {"600835": 18.56})
        assert close.call_args.kwargs["shares"] == 500

    def test_an_absurd_fraction_is_clamped(self):
        """Trimming 99% is a close wearing a trim's label."""
        with patch.object(exit_decision, "close_position",
                          return_value=True) as close:
            exit_decision.apply(
                [{"code": "600835", "action": "trim", "reason": "x",
                  "fraction": 5}], self.POS, {"600835": 18.56})
        assert close.call_args.kwargs["shares"] == 900

    def test_add_passes_the_stated_size(self):
        with patch("alpha_agents.data.portfolio.add_to_position",
                   return_value={"shares": 500, "avg_price": 18.5,
                                 "total_shares": 1500}) as add:
            alerts = exit_decision.apply(
                [{"code": "600835", "action": "add", "reason": "主线加速",
                  "size_pct": 0.02}], self.POS, {"600835": 18.56})
        assert add.call_args.kwargs["size_pct"] == 0.02
        assert alerts[0]["type"] == "agent_add"

    def test_an_add_with_no_room_is_not_an_alert(self):
        """Refusing an add is a legitimate answer, not a failure."""
        with patch("alpha_agents.data.portfolio.add_to_position",
                   return_value=None):
            assert exit_decision.apply(
                [{"code": "600835", "action": "add", "reason": "x"}],
                self.POS, {"600835": 18.56}) == []

    def test_a_trim_too_small_to_execute_holds(self):
        """Below one lot the honest outcome is holding, not a silent exit."""
        with patch.object(exit_decision, "close_position",
                          return_value=False) as close:
            assert exit_decision.apply(
                [{"code": "600835", "action": "trim", "reason": "x"}],
                [{"id": 7, "code": "600835", "name": "X", "shares": 100,
                  "open_price": 18.4}], {"600835": 18.56}) == []
        assert close.called
