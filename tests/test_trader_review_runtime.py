"""T6: review decisions into candidates, never straight into active rules."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.data import (
    memory_store, trader_learning as L, trader_state_store,
)
from alpha_agents.evolution import close_day, trader_review as R
from alpha_agents.trader import (
    Action, DecisionHorizon, EvidenceScope, Timeframe, TraderDecision,
    TraderState,
)

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    db = memory_store._get_conn()
    yield db
    db.close()
    memory_store._local.conn = None


def decision(
    decision_id: str, action: Action, *, code="600001",
    timeframe=Timeframe.DAILY, scope=EvidenceScope.LIVE_DAILY,
):
    return TraderDecision(
        decision_id=decision_id,
        made_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        action=action,
        code=code,
        thesis_id=None,
        confidence=0.6,
        reasoning="stored reason",
        timeframe=timeframe,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=scope,
    )


def persisted_state(conn, decisions):
    state = TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 25, 15, 0, tzinfo=TZ),
        recent_decisions=tuple(decisions),
    )
    trader_state_store.save(state, run_id="live", conn=conn)
    return state


def patch_model(monkeypatch, payload):
    import alpha_agents.model_factory as MF

    class Result:
        final_output = json.dumps(payload, ensure_ascii=False)

    async def run_agent(*args, **kwargs):
        return Result()

    monkeypatch.setattr(MF, "run_agent", run_agent)


def test_wait_hold_and_buy_are_all_reviewable(conn, monkeypatch):
    persisted_state(conn, [
        decision("buy-1", Action.BUY),
        decision("wait-1", Action.WAIT),
        decision("hold-1", Action.HOLD, code=None),
    ])
    patch_model(monkeypatch, {
        "reviews": [
            {
                "decision_id": "buy-1",
                "decision_quality": "good",
                "execution_quality": "good",
                "outcome_quality": "unknown",
                "reason": "当时条件满足",
                "lesson": None,
            },
            {
                "decision_id": "wait-1",
                "decision_quality": "good",
                "execution_quality": "unknown",
                "outcome_quality": "bad",
                "reason": "等待条件过严导致错过",
                "lesson": {
                    "claim": "强主题突破时不要只等深回踩",
                    "action": "adjust",
                    "applicable_context": "强主题突破",
                    "support_count": 1,
                    "counterexample_count": 0,
                    "confidence": 0.55,
                },
            },
            {
                "decision_id": "hold-1",
                "decision_quality": "uncertain",
                "execution_quality": "unknown",
                "outcome_quality": "unknown",
                "reason": "市场事实不足",
                "lesson": None,
            },
        ]
    })

    got = asyncio.run(R.review_decisions(
        conn, trader_id="default", day="2026-09-25", model="stub",
        facts="market facts", record="execution facts", run_id="live"))
    assert got == {"decision_reviews": 3, "lesson_candidates": 1}
    rows = L.decision_reviews(
        run_id="live", trader_id="default", conn=conn)
    assert {row["decision_id"] for row in rows} == {
        "buy-1", "wait-1", "hold-1"}


def test_lesson_metadata_is_inherited_from_the_decision(conn, monkeypatch):
    persisted_state(conn, [decision(
        "wait-1", Action.WAIT, timeframe=Timeframe.DAILY,
        scope=EvidenceScope.REPLAY_DAILY)])
    patch_model(monkeypatch, {
        "reviews": [{
            "decision_id": "wait-1",
            "decision_quality": "bad",
            "execution_quality": "unknown",
            "outcome_quality": "bad",
            "reason": "错过",
            "lesson": {
                "claim": "更早重新评估",
                "action": "adjust",
                "applicable_context": "daily replay",
                # The model is not allowed to relabel evidence scope/timeframe;
                # those fields are intentionally absent from its schema.
                "support_count": 1,
                "counterexample_count": 0,
                "confidence": 0.6,
            },
        }]
    })
    got = asyncio.run(R.review_decisions(
        conn, trader_id="default", day="2026-09-25", model="stub",
        run_id="live"))
    assert got["lesson_candidates"] == 1
    [row] = L.lesson_candidates(
        run_id="live", trader_id="default", conn=conn)
    assert row["evidence_timeframe"] == "1d"
    assert row["decision_horizon"] == "3-5d"
    assert row["evidence_scope"] == "replay_daily"


def test_exact_review_retry_is_idempotent(conn, monkeypatch):
    persisted_state(conn, [decision("wait-1", Action.WAIT)])
    patch_model(monkeypatch, {
        "reviews": [{
            "decision_id": "wait-1",
            "decision_quality": "good",
            "execution_quality": "unknown",
            "outcome_quality": "unknown",
            "reason": "合理等待",
            "lesson": None,
        }]
    })
    first = asyncio.run(R.review_decisions(
        conn, trader_id="default", day="2026-09-25", model="stub",
        run_id="live"))
    second = asyncio.run(R.review_decisions(
        conn, trader_id="default", day="2026-09-25", model="stub",
        run_id="live"))
    assert first["decision_reviews"] == 1
    assert second["decision_reviews"] == 0
    assert len(L.decision_reviews(
        run_id="live", trader_id="default", conn=conn)) == 1


def test_review_store_is_append_only(conn):
    L.init_schema(conn)
    decision_row = decision("d1", Action.BUY).as_dict()
    assert L.save_decision_review(
        run_id="live", trader_id="default", decision=decision_row,
        review_date="2026-09-25",
        decision_quality="good", execution_quality="unknown",
        outcome_quality="unknown", reason="r", payload={}, conn=conn)
    conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE trader_decision_reviews SET reason='x'")
    conn.rollback()


def test_close_day_never_rewrites_legacy_handbook(conn, monkeypatch):
    from alpha_agents.evolution import handbook, market_review, trade_review

    async def reviewed(*args, **kwargs):
        return 0

    async def reviewed_decisions(*args, **kwargs):
        return {"decision_reviews": 0, "lesson_candidates": 0}

    monkeypatch.setattr(trade_review, "review_closed", reviewed)
    monkeypatch.setattr(R, "review_decisions", reviewed_decisions)
    monkeypatch.setattr(R, "lessons_from_trade_reviews", lambda *a, **k: 0)
    monkeypatch.setattr(market_review, "missed", lambda *a, **k: "")
    monkeypatch.setattr(handbook, "load", lambda *a, **k: "")

    async def forbidden(*args, **kwargs):
        raise AssertionError("close_day must not create active handbook rules")

    monkeypatch.setattr(handbook, "consolidate", forbidden)

    hist = sqlite3.connect(":memory:")
    got = asyncio.run(close_day.review_day(
        conn, hist, trader_id="default", trader=SimpleNamespace(),
        day="2026-09-25", model="stub", facts_text="",
        record_text=""))
    hist.close()
    assert got["handbook"] == 0
    assert got["handbook_failed"] == 0


def test_trade_review_next_time_becomes_candidate_not_rule(conn, monkeypatch):
    from alpha_agents.evolution import trade_review

    monkeypatch.setattr(
        trade_review, "reviews_for",
        lambda *a, **k: [(
            {
                "position_id": 9, "code": "600001", "theme": "算力",
                "close_date": "2026-09-25", "return_pct": -2.0,
            },
            {"next_time": "强主题里等待条件不要过深"},
        )])
    monkeypatch.setattr(
        "alpha_agents.evolution.replay_mode.replay_process",
        lambda: False)
    n = R.lessons_from_trade_reviews(
        conn, trader_id="default", day="2026-09-25", run_id="live")
    assert n == 1
    [row] = L.lesson_candidates(
        run_id="live", trader_id="default", conn=conn)
    assert row["source_type"] == "trade_review"
    assert row["claim"] == "强主题里等待条件不要过深"
    assert row["evidence_timeframe"] == "1d"


def test_the_models_grades_and_counts_are_dropped(conn, monkeypatch):
    """The reviewing model explains; the market grades. Whatever quality or
    count the model still writes is not stored as evidence."""
    persisted_state(conn, [decision("buy-1", Action.BUY)])
    patch_model(monkeypatch, {"reviews": [{
        "decision_id": "buy-1",
        "decision_quality": "good",
        "execution_quality": "good",
        "outcome_quality": "good",
        "reason": "当时条件满足",
        "lesson": {"claim": "突破时追入", "action": "buy",
                   "applicable_context": "突破",
                   "support_count": 999, "counterexample_count": 0,
                   "confidence": 1.0},
    }]})
    asyncio.run(R.review_decisions(
        conn, trader_id="default", day="2026-09-25", model="stub",
        run_id="live"))
    [review] = L.decision_reviews(run_id="live", trader_id="default",
                                  conn=conn)
    assert {review["decision_quality"], review["execution_quality"],
            review["outcome_quality"]} == {R.UNGRADED}
    [candidate] = L.lesson_candidates(run_id="live", trader_id="default",
                                      conn=conn)
    assert candidate["support_count"] == 0
    assert candidate["confidence"] == 0.0


def test_the_prompt_asks_for_no_grade():
    for field in ("decision_quality", "outcome_quality", "support_count",
                  "counterexample_count", "confidence"):
        assert field not in R._INSTRUCTIONS
