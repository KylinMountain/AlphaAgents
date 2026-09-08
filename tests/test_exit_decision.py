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
                        "reason": "主线归档，逻辑已破", "confidence": "high"}]

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
        """A tighter account wants a tighter floor without a code change."""
        monkeypatch.setattr(portfolio, "HARD_STOP_PCT", 5.0)
        assert portfolio._is_hard_exit({"theme": ""}, -5.1)
        assert not portfolio._is_hard_exit({"theme": ""}, -4.9)


class TestRunDegradesToHold:
    @pytest.mark.asyncio
    async def test_no_positions_is_a_no_op(self):
        with patch.object(exit_decision, "get_open_positions", return_value=[]):
            assert await exit_decision.run({}, []) == []

    @pytest.mark.asyncio
    async def test_an_agent_failure_holds(self, caplog):
        pos = [{"id": 1, "code": "600835", "name": "上海机电",
                "open_price": 18.4, "theme": "", "reason": ""}]
        with patch.object(exit_decision, "get_open_positions", return_value=pos), \
             patch.object(exit_decision, "_news_for_theme", return_value=[]), \
             patch.object(exit_decision, "decide",
                          side_effect=RuntimeError("model down")), \
             patch.object(exit_decision, "close_position") as close, \
             caplog.at_level("WARNING"):
            assert await exit_decision.run({"600835": 18.5}, []) == []
        close.assert_not_called()
        assert any("holding all" in r.getMessage() for r in caplog.records)
