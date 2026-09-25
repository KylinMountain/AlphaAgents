"""T4: intraday observations wake the same continuous Trader."""
import asyncio
import inspect
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from alpha_agents.agents import trader_runtime as C
from alpha_agents.pipeline.tasks import intraday_monitor as IM
from alpha_agents.trader import (
    Action, CompareOp, Condition, DecisionHorizon, EvidenceScope,
    Timeframe, TraderDecision, TraderState, WatchItem, WatchStatus,
)

TZ = ZoneInfo("Asia/Shanghai")


def at(hour=10, minute=40):
    return datetime(2026, 9, 25, hour, minute, tzinfo=TZ)


def trader():
    return SimpleNamespace(
        id="default", name="Default", note="", extra_prompt="",
        default_horizon_days=5,
    )


def waiting_state(*, price=30.0):
    watch = WatchItem(
        code="600001", status=WatchStatus.WATCHING,
        why="逻辑成立，等回踩",
        trigger_conditions=(
            Condition("price", CompareOp.LE, price, subject="600001"),),
        invalidation_conditions=(), trigger_all=False,
        next_check=f"600001:price <= {price}",
        created_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        last_checked_at=None,
        evidence_timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.LIVE_DAILY,
    )
    from alpha_agents.trader import Observation, ObservationType
    candidate = Observation.create(
        observed_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        available_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        type=ObservationType.CANDIDATE,
        subjects=["600001", "算力"],
        data={"name": "甲", "theme": "算力", "reason": "主线"},
        source="morning_research",
        evidence_refs=["prediction:1"],
        timeframe=Timeframe.DAILY,
    )
    return TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        watchlist=(watch,), recent_observations=(candidate,))


def history():
    return [
        {"close": 10.0, "volume": 10000, "change_pct": 0.5}
        for _ in range(20)
    ]


def store(monkeypatch, initial):
    current = {"state": initial}
    saved = []

    monkeypatch.setattr(
        C.trader_state_store, "load_latest",
        lambda **kwargs: current["state"])

    def save(value, **kwargs):
        current["state"] = value
        saved.append(value)
        return True

    monkeypatch.setattr(C.trader_state_store, "save", save)
    return current, saved


def test_quiet_price_above_wait_level_does_not_call_model(monkeypatch):
    monkeypatch.setattr(C, "_logical_now", lambda: at())
    current, saved = store(monkeypatch, waiting_state())
    monkeypatch.setattr(C.mh, "get_local_history", lambda *a, **k: history())

    async def forbidden(**kwargs):
        raise AssertionError("unchanged WAIT must not call planner")

    monkeypatch.setattr(C.t1_decider, "propose", forbidden)
    got = asyncio.run(C.plan_intraday(
        [], trader(), prices={"600001": 31.0, "600001_chg": 1.0}))

    assert got["status"] == "observed"
    assert got["decisions"] == []
    assert current["state"].watchlist[0].status == WatchStatus.WATCHING


def test_price_cross_wakes_same_watch_and_buy_uses_intent(monkeypatch):
    monkeypatch.setattr(C, "_logical_now", lambda: at())
    current, saved = store(monkeypatch, waiting_state())
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C.mh, "get_local_history", lambda *a, **k: history())
    monkeypatch.setattr(C.feedback, "inject_portfolio", lambda **kwargs: "")
    monkeypatch.setattr(C, "_write_execution_thesis", lambda *a, **k: 91)
    submitted = []
    monkeypatch.setattr(
        C, "create_pending_order",
        lambda **kwargs: submitted.append(kwargs) or 321)

    async def propose(**kwargs):
        assert kwargs["phase"] == "intraday"
        assert kwargs["information_cutoff"] == "2026-09-25 10:40:00+08:00"
        assert kwargs["panel"][0]["code"] == "600001"
        assert kwargs["panel"][0]["close"] == 29.8
        assert kwargs["research_packet"]["triggered_watch"] == ["600001"]
        return {
            "orders": [{
                "code": "600001", "entry_low": None,
                "entry_high": 30.0, "stop_loss": 28.5,
                "target_price": 34.0, "reason": "回踩条件已满足",
            }],
            "watch": [], "rejected": [], "refused": [],
            "parse_error": None, "decision_status": "ordered",
            "no_trade_reason": "", "decision_id": "intraday-1",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_intraday(
        [], trader(), prices={"600001": 29.8, "600001_chg": -0.5}))

    assert got["reevaluate_subjects"] == ["600001"]
    assert got["decisions"][0]["action"] == Action.BUY.value
    assert got["placed"][0]["order_id"] == 321
    assert submitted[0]["source"] == "trader_runtime_intraday"
    assert current["state"].watchlist[0].status == WatchStatus.CONVERTED
    assert len(saved) == 2, "trigger state then committed decision"


def test_new_intraday_candidate_can_become_wait(monkeypatch):
    base = TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 25, 10, 35, tzinfo=TZ))
    monkeypatch.setattr(C, "_logical_now", lambda: at())
    current, _ = store(monkeypatch, base)
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C.mh, "get_local_history", lambda *a, **k: history())
    monkeypatch.setattr(C.feedback, "inject_portfolio", lambda **kwargs: "")

    async def propose(**kwargs):
        return {
            "orders": [],
            "watch": [{
                "code": "600002", "reason": "等突破",
                "confidence": 0.6,
                "next_check": [{
                    "metric": "price", "op": ">=", "value": 20.5,
                    "subject": "600002",
                }],
                "invalidations": [],
            }],
            "rejected": [], "refused": [], "parse_error": None,
            "decision_status": "waiting", "no_trade_reason": "",
            "decision_id": "intraday-2",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_intraday(
        [{"code": "600002", "name": "乙", "theme": "机器人",
          "score": 80, "change_pct": 2.0}],
        trader(),
        prices={"600002": 20.0, "600002_chg": 2.0}))

    assert got["status"] == "waiting"
    assert current["state"].watchlist[0].code == "600002"
    assert current["state"].watchlist[0].status == WatchStatus.WATCHING


def test_same_day_rejected_candidate_is_not_reasked_without_watch_trigger(monkeypatch):
    rejected = TraderDecision(
        decision_id="morning-reject",
        made_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        action=Action.REJECT,
        code="600002", thesis_id=None, confidence=0.5,
        reasoning="逻辑不足", timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.LIVE_DAILY)
    base = TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 25, 10, 35, tzinfo=TZ),
        recent_decisions=(rejected,))
    monkeypatch.setattr(C, "_logical_now", lambda: at())
    store(monkeypatch, base)

    async def forbidden(**kwargs):
        raise AssertionError("same-day rejected candidate must not loop")

    monkeypatch.setattr(C.t1_decider, "propose", forbidden)
    got = asyncio.run(C.plan_intraday(
        [{"code": "600002", "name": "乙", "theme": "机器人",
          "score": 80, "change_pct": 2.0}],
        trader(), prices={"600002": 20.0, "600002_chg": 2.0}))
    assert got["status"] == "observed"
    assert got["decisions"] == []


def test_intraday_phase_renders_current_snapshot_not_t_minus_one():
    from alpha_agents.agents import t1_decider as D

    rendered = D.format_panel([{
        "code": "600001", "name": "甲", "close": 29.8,
        "change_pct": -0.5, "adv20": 10000,
    }], phase="intraday")
    assert "当前价" in rendered
    assert "今日涨幅" in rendered
    assert "T-1 收盘" not in rendered


def test_intraday_cutoff_must_be_inside_market_session():
    from alpha_agents.agents import t1_decider as D
    import pytest

    D._validate_information_cutoff(
        "2026-09-25", "intraday", "2026-09-25 10:40:00")
    with pytest.raises(D.DeciderError, match="inside"):
        D._validate_information_cutoff(
            "2026-09-25", "intraday", "2026-09-25 09:20:00")


def test_production_intraday_no_longer_uses_entry_pricing_as_order_owner():
    source = inspect.getsource(IM._save_intraday_recommendations)
    assert "plan_intraday(" in source
    assert "place_order=False" in source
    assert "_price_for(t" not in source


def test_watch_check_occurs_before_no_anomaly_early_return():
    source = inspect.getsource(IM.run_intraday_monitor)
    assert source.index("plan_intraday(") < source.index("if not has_anomaly:")


def test_intraday_prediction_recorder_can_be_research_only(monkeypatch):
    monkeypatch.setattr(
        IM, "save_prediction", lambda **kwargs: 77)
    monkeypatch.setattr(
        IM, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("research-only recorder must not place order")))
    monkeypatch.setattr(
        "alpha_agents.evolution.playbook.change_band",
        lambda value: "1_3")

    pred_id = IM._record_intraday_pick(
        trader(),
        {"code": "600002", "name": "乙", "theme": "机器人",
         "score": 80, "change_pct": 2.0, "reason": "异动"},
        "600002", "2026-09-25", "intraday", "high", 20.0,
        {"600002": 20.0, "600002_chg": 2.0},
        {}, "actionable", place_order=False)
    assert pred_id == 77


def test_duplicate_sector_candidates_become_one_trader_subject():
    got = IM._dedupe_actionable_by_code([
        {"code": "600001", "theme": "AI算力", "score": 70},
        {"code": "600001", "theme": "液冷", "score": 85},
        {"code": "600002", "theme": "机器人", "score": 60},
    ])
    assert [item["code"] for item in got] == ["600001", "600002"]
    assert got[0]["theme"] == "液冷"
    assert got[0]["alternate_themes"] == ["AI算力"]


def test_theme_flow_invalidation_can_wake_a_wait_without_price_trigger(monkeypatch):
    watch = WatchItem(
        code="600001", status=WatchStatus.WATCHING,
        why="主题仍强，等价格",
        trigger_conditions=(
            Condition("price", CompareOp.LE, 25.0, subject="600001"),),
        invalidation_conditions=(
            Condition("net_flow", CompareOp.LT, 0, subject="算力"),),
        trigger_all=False,
        next_check="600001:price <= 25",
        created_at=datetime(2026, 9, 25, 9, 5, tzinfo=TZ),
        last_checked_at=None,
        evidence_timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.LIVE_DAILY,
    )
    base = TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 25, 10, 35, tzinfo=TZ),
        watchlist=(watch,))
    monkeypatch.setattr(C, "_logical_now", lambda: at())
    current, _ = store(monkeypatch, base)
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C.mh, "get_local_history", lambda *a, **k: history())
    monkeypatch.setattr(C.feedback, "inject_portfolio", lambda **kwargs: "")

    async def propose(**kwargs):
        return {
            "orders": [], "watch": [], "refused": [],
            "rejected": [{
                "code": "600001", "reason": "主题资金转负",
                "rule_ids": [],
            }],
            "parse_error": None, "decision_status": "abstained",
            "no_trade_reason": "原等待逻辑失效",
            "decision_id": "intraday-flow-reject",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_intraday(
        [], trader(),
        prices={"600001": 31.0, "600001_chg": 1.0},
        market_view={
            "sector_flows": {"算力": -3.2},
            "sector_ranks": {"算力": 18},
            "breadth_ratio": 0.45,
        }))

    assert got["reevaluate_subjects"] == ["600001"]
    assert got["decisions"][0]["action"] == Action.REJECT.value
    assert current["state"].watchlist[0].status == WatchStatus.REJECTED


def test_t4_wait_vocabulary_refuses_fake_volume_ratio():
    from alpha_agents.agents import t1_decider as D

    verdict = D.parse_orders(
        '{"orders":[],"watch":[{"code":"600001","reason":"等量",'
        '"next_check":[{"metric":"volume_ratio","op":">=","value":1.5}],'
        '"invalidations":[]}]}',
        {"600001"})
    assert verdict["decision_status"] == "incomplete"
