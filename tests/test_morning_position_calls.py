"""晨扫对已有持仓的判断，本来是产出了就扔掉的。

开盘前那一小时正是决定「昨天的仓位今天怎么办」的时刻——隔夜消息、外盘、
主线变化都在这时明朗。agent 看得见持仓（inject_portfolio），也确实形成了
判断，但 RECOMMENDATIONS 只能表达「买入一个新标的」。于是一个减仓决策
以 theme="持仓管理" 的推荐形式出现，被 resolve_theme 丢弃。

给了信息没给词汇。
"""

import json

import pytest

from alpha_agents.pipeline.tasks import exit_decision
from alpha_agents.pipeline.tasks.morning_scan import _save_position_calls


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


def report(payload: str) -> str:
    return f"晨报正文\n\n<!--POSITIONS\n{payload}\nPOSITIONS-->\n"


TODAY = "2026-09-09"


class TestParsing:
    def test_stores_a_well_formed_block(self, store):
        n = _save_position_calls(report(
            '[{"code":"600637","action":"trim","fraction":0.5,'
            '"reason":"ROE仅1.42%且不在任何活跃主线上"}]'), TODAY)
        assert n == 1
        calls = exit_decision.pending_morning_calls(TODAY)
        assert calls[0]["action"] == "trim"
        assert calls[0]["fraction"] == 0.5

    def test_no_block_is_not_an_error(self, store):
        """No positions means nothing to say about them."""
        assert _save_position_calls("晨报正文，无持仓", TODAY) == 0

    def test_malformed_json_stores_nothing(self, store, caplog):
        with caplog.at_level("WARNING"):
            assert _save_position_calls(report('[{"code":,,}]'), TODAY) == 0
        assert any("malformed" in r.getMessage() for r in caplog.records)

    def test_an_unknown_action_is_dropped(self, store):
        assert _save_position_calls(report(
            '[{"code":"600637","action":"换股","reason":"x"}]'), TODAY) == 0

    def test_a_sell_with_no_reason_is_refused(self, store, caplog):
        """The reason is what the review grades. Without it the decision
        teaches nothing, which is why the agent was given the channel."""
        with caplog.at_level("WARNING"):
            assert _save_position_calls(report(
                '[{"code":"600637","action":"sell","reason":""}]'), TODAY) == 0

    def test_hold_needs_no_reason(self, store):
        assert _save_position_calls(report(
            '[{"code":"600637","action":"hold","reason":""}]'), TODAY) == 1


class TestConsumedOnce:
    def test_the_second_read_is_empty(self, store):
        """A trim must not re-run every five minutes on a market that has
        already moved past it."""
        _save_position_calls(report(
            '[{"code":"600637","action":"trim","reason":"减半"}]'), TODAY)
        assert len(exit_decision.pending_morning_calls(TODAY)) == 1
        assert exit_decision.pending_morning_calls(TODAY) == []

    def test_another_day_sees_nothing(self, store):
        _save_position_calls(report(
            '[{"code":"600637","action":"sell","reason":"x"}]'), TODAY)
        assert exit_decision.pending_morning_calls("2026-09-10") == []

    def test_a_rerun_replaces_rather_than_appends(self, store):
        """Two morning scans in one day is one verdict, not two."""
        _save_position_calls(report(
            '[{"code":"600637","action":"sell","reason":"第一次"}]'), TODAY)
        _save_position_calls(report(
            '[{"code":"600637","action":"hold","reason":""}]'), TODAY)
        calls = exit_decision.pending_morning_calls(TODAY)
        assert len(calls) == 1 and calls[0]["action"] == "hold"


class TestShape:
    def test_matches_what_apply_expects(self, store):
        """Same vocabulary as the intraday decision, so one applier runs
        both — a second execution path would drift from the first."""
        _save_position_calls(report(
            '[{"code":"600637","action":"trim","fraction":0.4,"reason":"x"}]'),
            TODAY)
        call = exit_decision.pending_morning_calls(TODAY)[0]
        assert set(call) >= {"code", "action", "reason", "fraction", "size_pct"}
        assert call["action"] in exit_decision.VALID_ACTIONS
