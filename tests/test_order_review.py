"""A weakened theme wakes the agent about its resting order, it does not pull it.

Live 2026-09-10 → 09-23 about 25 of the two traders' orders were cancelled by
``theme_gate`` — some minutes after placement — without the agent being
asked. These pin the replacement: evidence to the agent, answer applied.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import portfolio as P
from alpha_agents.data import thesis as T
from alpha_agents.data import trader as TR
from alpha_agents.pipeline.tasks import order_review as OR

ARCHIVED = {"name": "t", "status": "archived", "strength": 2,
            "daily_score": -1, "core_stocks": "[]"}
ACTIVE = dict(ARCHIVED, status="active", strength=6, daily_score=1)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
                        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    d = tmp_path / "traders"
    d.mkdir()
    (d / "slow.yaml").write_text("id: slow\nname: 回调派\ncapital: 500000\n",
                                 encoding="utf-8")
    monkeypatch.setattr(TR, "TRADERS_DIR", d, raising=False)
    OR._asked.clear()
    yield
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _theme(row):
    return [patch("alpha_agents.data.memory_store.get_active_themes",
                  return_value=[row]),
            patch("alpha_agents.data.memory_store.get_theme_by_name",
                  return_value=row),
            patch("alpha_agents.data.portfolio.get_theme_by_name",
                  return_value=row),
            patch("alpha_agents.data.theme_gate.get_theme_by_name",
                  return_value=row),
            patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
                  return_value={"phase": "修复",
                                "strategy": {"max_exposure_pct": 100}})]


class _Patched:
    def __init__(self, row):
        self.ps = _theme(row)

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


def _order(with_thesis=True):
    thesis_id = None
    if with_thesis:
        thesis_id = T.create(T.Thesis(code="600000", name="A", theme="t",
                                      claim="主线在流入", trader_id="slow"))
    with _Patched(ACTIVE):
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=9.5, stop_loss=8.5, source="morning",
            reason="A", trader_id="slow", thesis_id=thesis_id)
    assert oid is not None
    return oid, thesis_id


def _status(oid):
    return memory_store._get_conn().execute(
        "SELECT status, close_reason FROM virtual_portfolio WHERE id=?",
        (oid,)).fetchone()


def _check(wake):
    # Above the zone (no fill) but under the 5% run-away line, so only the gate speaks.
    with _Patched(ARCHIVED):
        return P.check_pending_orders({"600000": 9.8}, "2026-01-05",
                                      trader_id="slow", wake_agent=wake)


class TestTheGateBecomesASignal:
    def test_an_order_with_a_thesis_is_kept_and_raised(self, book):
        oid, tid = _order()
        alerts = _check(wake=True)
        assert _status(oid)["status"] == "pending"
        sig = [a for a in alerts if a["type"] == "order_signal"]
        assert len(sig) == 1
        assert sig[0]["order_id"] == oid and sig[0]["thesis_id"] == tid
        assert sig[0]["reason"].startswith("主线已archived")

    def test_without_wake_it_is_cancelled_as_before(self, book):
        oid, _ = _order()
        _check(wake=False)
        row = _status(oid)
        assert row["status"] == "cancelled"
        assert row["close_reason"].startswith("主线已archived")

    def test_an_order_with_no_thesis_has_no_plan_to_consult(self, book):
        oid, _ = _order(with_thesis=False)
        _check(wake=True)
        assert _status(oid)["status"] == "cancelled"


def _run(signals, reply):
    async def fake(context, trader=None, **kw):
        return OR.parse(reply)
    with patch.object(OR, "decide", fake):
        return asyncio.run(OR.run(signals, {"600000": 10.0}, "2026-01-05",
                                  trader_id="slow"))


class TestTheAgentAnswers:
    def test_cancel_is_applied_with_the_agents_reason(self, book):
        oid, tid = _order()
        sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
        alerts = _run(sig, f'```json\n[{{"order_id": {oid}, "action": "cancel", '
                           f'"reason": "资金5日净流出-30亿，逻辑已破"}}]\n```')
        row = _status(oid)
        assert row["status"] == "cancelled"
        assert row["close_reason"] == "agent撤单: 资金5日净流出-30亿，逻辑已破"
        assert alerts and alerts[0]["type"] == "cancelled"
        assert T.get_by_id(tid).checkpoints[-1]["kind"] == "theme_gate"

    def test_keep_leaves_it_resting_and_notes_the_thesis(self, book):
        oid, tid = _order()
        sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
        _run(sig, f'[{{"order_id": {oid}, "action": "keep", '
                  f'"reason": "表归档但资金仍流入"}}]')
        assert _status(oid)["status"] == "pending"
        point = T.get_by_id(tid).checkpoints[-1]
        assert "agent keep" in point["observation"]

    def test_an_unreadable_reply_keeps_the_order(self, book):
        oid, _ = _order()
        sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
        assert _run(sig, "我觉得还行") == []
        assert _status(oid)["status"] == "pending"

    def test_a_failed_call_keeps_the_order(self, book):
        oid, _ = _order()
        sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]

        async def boom(*a, **k):
            raise RuntimeError("model down")
        with patch.object(OR, "decide", boom):
            assert asyncio.run(OR.run(sig, {}, "2026-01-05", "slow")) == []
        assert _status(oid)["status"] == "pending"

    def test_a_cancel_without_a_reason_is_not_applied(self):
        assert OR.parse('[{"order_id": 1, "action": "cancel", "reason": ""}]') == []


class TestOncePerDay:
    def test_the_same_reason_is_asked_once_a_day(self, book):
        oid, _ = _order()
        calls = []

        async def fake(context, trader=None, **kw):
            calls.append(context)
            return []
        with patch.object(OR, "decide", fake):
            for _ in range(3):
                sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
                asyncio.run(OR.run(sig, {}, "2026-01-05", "slow"))
            sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
            asyncio.run(OR.run(sig, {}, "2026-01-06", "slow"))
        assert len(calls) == 2

    def test_a_moving_score_is_the_same_reason(self):
        a = {"type": "order_signal", "order_id": 1,
             "reason": "主线明显走弱(t评分0.30<0.35)"}
        b = dict(a, reason="主线明显走弱(t评分0.22<0.35)")
        OR._asked.clear()
        OR._asked.add((1, "d", OR._kind(a["reason"])))
        assert OR.due([b], "d") == []


class TestTheAdmissionBarIsAdvice:
    """Same rule at the other end: creation refused an order whose theme
    the lifecycle had archived, before the agent's thesis was consulted."""

    def _place(self, wake, with_thesis=True):
        tid = (T.create(T.Thesis(code="600000", name="A", theme="t",
                                 claim="主线在流入", trader_id="slow"))
               if with_thesis else None)
        with _Patched(ARCHIVED):
            oid = P.create_pending_order(
                code="600000", name="A", theme="t", order_date="2026-01-05",
                entry_low=9.0, entry_high=9.5, stop_loss=8.5, source="morning",
                reason="A", trader_id="slow", thesis_id=tid, wake_agent=wake)
        return oid, tid

    def test_a_thesis_order_is_placed_and_the_bar_is_noted(self, book):
        oid, tid = self._place(wake=True)
        assert oid is not None and _status(oid)["status"] == "pending"
        point = T.get_by_id(tid).checkpoints[-1]
        assert point["kind"] == "theme_gate"
        assert "主线已archived" in point["observation"]

    def test_and_the_first_cycle_asks_the_agent(self, book):
        oid, _ = self._place(wake=True)
        sig = [a for a in _check(wake=True) if a["type"] == "order_signal"]
        assert [s["order_id"] for s in sig] == [oid]

    def test_without_wake_it_is_refused_as_before(self, book):
        assert self._place(wake=False)[0] is None

    def test_without_a_thesis_it_is_refused_as_before(self, book):
        assert self._place(wake=True, with_thesis=False)[0] is None


class TestTheLiveCallersPassTheFlag:
    def test_both_order_paths_hand_the_agent_the_last_word(self):
        # A NameError here is swallowed by the caller's except and every
        # order silently stops — so the wiring is pinned, not assumed.
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "alpha_agents/pipeline/tasks"
        for f in ("morning_scan.py", "intraday_monitor.py"):
            src = (root / f).read_text(encoding="utf-8")
            assert "wake_agent=exit_decision.enabled()" in src, f
        import alpha_agents.pipeline.tasks.morning_scan as m
        import alpha_agents.pipeline.tasks.intraday_monitor as i
        assert callable(m.exit_decision.enabled)
        assert callable(i.exit_decision.enabled)
