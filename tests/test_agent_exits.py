"""The agent's sell side, in a replay.

The first 20-day replay bought 11 positions and every one of them left
through its stop. That was not the model's choice: nothing asked it. The
runner called the agent once a day, about buying, and `_settle_exits` was
pure Python.

Two contracts matter here and both are about what the agent may *not* do:

* **The hard stop runs first.** A position that gapped through its stop is
  already closed when the agent is asked, so no amount of reasoning can
  argue a risk line away. This is the ordering in `run`, and it is the
  reason `decide_for_replay` must never be called before `_settle_exits`.
* **Every failure holds.** A timeout, an unreadable reply, a provider
  outage — none of them may sell. The mechanical stop is still underneath,
  so holding on error cannot run the book down.
"""

from __future__ import annotations

import asyncio

import pytest

from alpha_agents.pipeline.tasks import exit_decision as ED


POSITIONS = [
    {"id": 1, "code": "600001", "name": "甲", "shares": 100,
     "open_price": 10.0, "stop_loss": 9.0, "target_price": 12.0,
     "holding_days": 2, "theme": "半导体", "reason": "主线启动"},
]
PRICES = {"600001": 11.0}


class TestTheContextTellsTheAgentWhereItStands:
    def test_it_carries_cost_price_and_pnl(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[])
        assert "成本10.00" in ctx and "现价11.00" in ctx
        assert "+10.00%" in ctx

    def test_it_says_so_when_a_replay_has_no_news(self):
        """`None` means "this run supplied no news", which is not the same
        as "searched and found nothing" — a replay has no live index."""
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={})
        assert "未提供" in ctx
        assert "不等于没有消息" in ctx

    def test_a_live_caller_still_gets_the_live_wording(self, monkeypatch):
        monkeypatch.setattr(ED, "_news_for_theme", lambda t: [])
        ctx = ED.build_context(POSITIONS, PRICES, signals=[])
        assert "近期快讯: 无" in ctx
        assert "未提供" not in ctx

    def test_supplied_news_is_used_verbatim(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={"半导体": ["[09:10] 某快讯"]})
        assert "[09:10] 某快讯" in ctx


class TestEveryFailureHolds:
    def _run(self, monkeypatch, *, raises=None, reply=""):
        async def _fake_decide(context, trader=None, **kw):
            if raises:
                raise raises
            return ED.parse_decisions(reply)
        monkeypatch.setattr(ED, "decide", _fake_decide)
        return asyncio.run(ED.decide_for_replay(
            POSITIONS, PRICES, day="2026-01-05", news_by_theme={}))

    def test_a_timeout_holds(self, monkeypatch):
        assert self._run(monkeypatch, raises=asyncio.TimeoutError()) == []

    def test_a_provider_error_holds(self, monkeypatch):
        assert self._run(monkeypatch, raises=RuntimeError("boom")) == []

    def test_an_unreadable_reply_is_not_a_sell(self, monkeypatch):
        """Nothing means hold. An exit that fires because the model wrote
        broken JSON is worse than one that never fires."""
        assert self._run(monkeypatch, reply="我觉得该卖但我不写 JSON") == []

    def test_no_positions_makes_no_call(self, monkeypatch):
        called = []

        async def _fake_decide(*a, **kw):
            called.append(1)
            return []
        monkeypatch.setattr(ED, "decide", _fake_decide)
        out = asyncio.run(ED.decide_for_replay(
            [], PRICES, day="2026-01-05", news_by_theme={}))
        assert out == [] and called == []


class TestTheReplayPassesNoTools:
    def test_a_replay_model_and_no_tools_reach_decide(self, monkeypatch):
        """Every live tool answers from *now*. A replay that let
        `search_news` through would decide a 2026-08 day with 2026-09
        flashes. The buy side never had this problem because it passes no
        tools at all."""
        seen = {}

        async def _fake_decide(context, trader=None, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(ED, "decide", _fake_decide)
        asyncio.run(ED.decide_for_replay(
            POSITIONS, PRICES, day="2026-01-05", news_by_theme={},
            model=object()))
        assert seen.get("tools") == []
        assert seen.get("model") is not None


class TestParseDecisionsRefusesWhatItCannotGrade:
    def test_an_order_without_a_reason_is_ignored(self):
        """The whole point of moving the decision to the agent was to get a
        reason worth grading at review."""
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"sell",'
            '"reason":""}] -->')
        assert out == []

    def test_hold_needs_no_reason(self):
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"hold",'
            '"reason":""}] -->')
        assert [d["action"] for d in out] == ["hold"]

    def test_an_unknown_action_is_ignored(self):
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"yolo",'
            '"reason":"x"}] -->')
        assert out == []

    def test_a_malformed_block_returns_nothing(self):
        assert ED.parse_decisions("no block here") == []
