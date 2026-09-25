"""T8: close review keeps replay learning in its own runtime namespace."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3

from alpha_agents.evolution import close_day


def test_close_review_propagates_the_replay_run_id(monkeypatch):
    from alpha_agents.evolution import trade_review, trader_learning, trader_review

    seen = {}

    async def closed(*args, **kwargs):
        seen["trade_timeout"] = kwargs["timeout"]
        return 0

    async def decisions(*args, **kwargs):
        seen["decisions"] = kwargs["run_id"]
        seen["decision_timeout"] = kwargs["timeout"]
        return {"decision_reviews": 0, "lesson_candidates": 0}

    def trade_lessons(*args, **kwargs):
        seen["trade"] = kwargs["run_id"]
        return 0

    def advance(*args, **kwargs):
        seen["advance"] = kwargs["run_id"]
        return {"lessons_created": 0, "rules_created": 0}

    monkeypatch.setattr(trade_review, "review_closed", closed)
    monkeypatch.setattr(trader_review, "review_decisions", decisions)
    monkeypatch.setattr(trader_review, "lessons_from_trade_reviews", trade_lessons)
    monkeypatch.setattr(trader_learning, "advance", advance)

    conn = sqlite3.connect(":memory:")
    hist = sqlite3.connect(":memory:")
    try:
        asyncio.run(close_day.review_day(
            conn, hist, trader_id="default", trader=None, day="2026-09-25",
            model=None, facts_text="", review_market=False,
            run_id="replay-30d", model_timeout=17.5))
    finally:
        conn.close()
        hist.close()

    assert seen == {
        "decisions": "replay-30d",
        "trade": "replay-30d",
        "advance": "replay-30d",
        "trade_timeout": 17.5,
        "decision_timeout": 17.5,
    }


def test_replay_seals_position_decisions_and_syncs_book_after_fills():
    from scripts import walk_forward as WF

    exits = inspect.getsource(WF._agent_exits)
    assert exits.index("ED.commit_runtime_decisions(") < exits.index("ED.apply(")
    assert "run_id=ctx.run_id" in exits

    loop = inspect.getsource(WF._run_window)
    assert loop.index("_sync_replay_positions(ctx, day, \"settle\")") < loop.index(
        "_agent_exits(ctx, day, phase)")
    assert loop.index("_sync_replay_positions(ctx, day, \"close\")") < loop.index(
        "with replay_as_of(day):")
