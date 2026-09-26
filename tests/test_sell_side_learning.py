"""Sell decisions read the Trader's position-horizon Lessons/Rules."""
from __future__ import annotations

import asyncio

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import trader_learning as D
from alpha_agents.evolution import trader_learning as L
from alpha_agents.pipeline.tasks import exit_decision as ED

POSITIONS = [{"id": 1, "code": "600001", "name": "甲", "shares": 100,
              "open_price": 10.0, "theme": "", "reason": "x"}]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "m.db",
                        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    db = memory_store._get_conn()
    yield db
    db.close()
    memory_store._local.conn = None


def _rule(conn, *, action, horizon, run="run-a"):
    for i in range(L.RULE_MIN_N):
        D.save_decision_outcome(
            run_id=run, trader_id="default", decision_id=f"{action}{i}",
            action=action, code="600001", decided_on="2026-01-05",
            base_date="2026-01-05", end_date="2026-01-12", horizon=5,
            forward_pct=-3.0, market_median_pct=0.0, excess_pct=-3.0,
            verdict="wrong" if action == "hold" else "right",
            tags=["off_5d_high"], evidence_timeframe="1d",
            decision_horizon=horizon, evidence_scope="replay_daily",
            conn=conn)
    L.advance(trader_id="default", as_of="2026-01-12", run_id=run, conn=conn)


def _context(monkeypatch, run="run-a") -> str:
    seen = {}

    async def _fake(context, trader=None, **kw):
        seen["context"] = context
        return []

    monkeypatch.setattr(ED, "decide", _fake)
    asyncio.run(ED.decide_for_replay(
        POSITIONS, {"600001": 9.5}, day="2026-01-13", news_by_theme={},
        model=object(), run_id=run))
    return seen["context"]


def test_a_position_rule_reaches_the_sell_decision(conn, monkeypatch):
    _rule(conn, action="hold", horizon="position")
    text = _context(monkeypatch)
    assert "RULE" in text and "持有" in text


def test_a_buy_rule_does_not(conn, monkeypatch):
    _rule(conn, action="buy", horizon="3-5d")
    assert "RULE" not in _context(monkeypatch)


def test_another_runs_rule_does_not(conn, monkeypatch):
    _rule(conn, action="hold", horizon="position", run="run-b")
    assert "RULE" not in _context(monkeypatch, run="run-a")
