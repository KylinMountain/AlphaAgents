"""T3c: production morning research is not the owner of the trade."""
import asyncio
import inspect
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.agents import trader_runtime as C
from alpha_agents.pipeline.tasks import morning_scan
from alpha_agents.trader import Action, TraderState, WatchStatus

TZ = ZoneInfo("Asia/Shanghai")


def trader():
    return SimpleNamespace(
        id="default", name="Default", note="", extra_prompt="",
        default_horizon_days=5,
    )


def fixed_panel():
    return [{
        "code": "600001", "name": "甲", "close": 10.0,
        "change_pct": 1.0, "adv20": 100000.0,
        "turnover_rate": 2.0, "consecutive_limits": None,
        "net_amount": None, "concepts": ["算力"], "_theme": "算力",
    }]


def setup_state_store(monkeypatch):
    saved = []

    monkeypatch.setattr(
        C.trader_state_store, "load_latest",
        lambda **kwargs: None)

    def save(value, **kwargs):
        saved.append((value, kwargs["run_id"]))
        return True

    monkeypatch.setattr(C.trader_state_store, "save", save)
    return saved


def test_non_trading_day_never_calls_planner(monkeypatch):
    monkeypatch.setattr(
        C, "_logical_now",
        lambda: datetime(2026, 9, 27, 9, 0, tzinfo=TZ))

    async def forbidden(**kwargs):
        raise AssertionError("planner must not run on research-only day")

    monkeypatch.setattr(C.t1_decider, "propose", forbidden)
    got = asyncio.run(C.plan_morning(
        [{"code": "600001", "theme": "算力"}],
        trader(), allow_new_risk=False))
    assert got["status"] == "research_only"
    assert got["placed"] == []


def test_after_open_never_adds_new_preopen_risk(monkeypatch):
    monkeypatch.setattr(
        C, "_logical_now",
        lambda: datetime(2026, 9, 25, 9, 31, tzinfo=TZ))

    async def forbidden(**kwargs):
        raise AssertionError("planner must not run after 09:30")

    monkeypatch.setattr(C.t1_decider, "propose", forbidden)
    got = asyncio.run(C.plan_morning(
        [{"code": "600001", "theme": "算力"}], trader()))
    assert got["status"] == "outside_preopen"
    assert got["placed"] == []


def test_wait_persists_watch_without_creating_order(monkeypatch):
    cutoff = datetime(2026, 9, 25, 9, 5, tzinfo=TZ)
    monkeypatch.setattr(C, "_logical_now", lambda: cutoff)
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C, "_panel", lambda recs, prev: fixed_panel())
    monkeypatch.setattr(C, "_portfolio_context", lambda *args: "")
    saved = setup_state_store(monkeypatch)
    monkeypatch.setattr(
        C, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("WAIT must not create an order")))

    seen = {}

    async def propose(**kwargs):
        seen.update(kwargs)
        return {
            "orders": [],
            "watch": [{
                "code": "600001",
                "reason": "等回踩",
                "confidence": 0.7,
                "next_check": [{
                    "metric": "price", "op": "<=", "value": 9.8,
                    "subject": "600001",
                }],
                "invalidations": [],
            }],
            "rejected": [],
            "refused": [],
            "parse_error": None,
            "decision_status": "waiting",
            "no_trade_reason": "",
            "decision_id": "capture-1",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_morning(
        [{"code": "600001", "name": "甲", "theme": "算力",
          "reason": "主线仍强"}],
        trader(), prediction_ids={"600001": 7}, allow_new_risk=True))

    assert got["status"] == "waiting"
    assert got["placed"] == []
    assert seen["information_cutoff"] == "2026-09-25 09:05:00+08:00"
    assert seen["tools"] == []
    assert seen["origin"] == "trader_runtime:morning"
    assert got["decisions"][0]["action"] == Action.WAIT.value
    final = saved[-1][0]
    assert final.watchlist[0].status == WatchStatus.WATCHING
    assert final.watchlist[0].code == "600001"
    assert final.recent_observations[-1].type.value == "candidate"


def test_buy_commits_state_then_enters_existing_intent_door(monkeypatch):
    cutoff = datetime(2026, 9, 25, 9, 5, tzinfo=TZ)
    monkeypatch.setattr(C, "_logical_now", lambda: cutoff)
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C, "_panel", lambda recs, prev: fixed_panel())
    monkeypatch.setattr(C, "_portfolio_context", lambda *args: "")
    saved = setup_state_store(monkeypatch)
    monkeypatch.setattr(C, "_write_execution_thesis", lambda *args: 88)
    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return 123

    monkeypatch.setattr(C, "create_pending_order", submit)

    async def propose(**kwargs):
        return {
            "orders": [{
                "code": "600001", "entry_low": None,
                "entry_high": 10.2, "stop_loss": 9.2,
                "target_price": 12.0, "reason": "条件满足",
                "conviction": 0.8,
            }],
            "watch": [], "rejected": [], "refused": [],
            "parse_error": None, "decision_status": "ordered",
            "no_trade_reason": "", "decision_id": "capture-2",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_morning(
        [{"code": "600001", "name": "甲", "theme": "算力"}],
        trader(), prediction_ids={"600001": 7}))

    assert got["decisions"][0]["action"] == Action.BUY.value
    assert got["placed"][0]["order_id"] == 123
    assert submitted[0]["source"] == "trader_runtime"
    assert submitted[0]["prediction_id"] == 7
    assert submitted[0]["thesis_id"] == 88
    # Candidate state and committed decision were sealed before execution.
    assert len(saved) == 3
    assert saved[-1][0].recent_decisions[-1].action == Action.BUY


def test_production_morning_explicitly_disables_legacy_order_owner():
    source = inspect.getsource(morning_scan._scan_for)
    assert "place_orders=False" in source
    assert "plan_morning(" in source
    assert "不回退旧下单路径" in source


def test_prediction_writer_can_record_without_placing_legacy_order(monkeypatch):
    called = []
    monkeypatch.setattr(morning_scan.clock, "today", lambda: "2026-09-25")
    monkeypatch.setattr(
        morning_scan, "get_stock_quotes_fn",
        lambda **kwargs: '{"quotes":[{"code":"600001","price":10.0}]}')
    monkeypatch.setattr(
        morning_scan, "build_decision_context",
        lambda **kwargs: {"hash": "x"})
    monkeypatch.setattr(morning_scan, "safe_active_themes", lambda: [])
    monkeypatch.setattr(morning_scan, "safe_market_regime", lambda: "")
    monkeypatch.setattr(morning_scan, "safe_sentiment_phase", lambda: "")
    monkeypatch.setattr(
        morning_scan, "_morning_relation_status",
        lambda *args, **kwargs: "not_checked")
    monkeypatch.setattr(
        morning_scan, "save_prediction",
        lambda **kwargs: called.append(kwargs) or 11)
    monkeypatch.setattr(
        morning_scan, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy order writer must stay dormant")))

    ids = morning_scan._save_recommendations_list(
        [{"code": "600001", "name": "甲", "theme": "算力",
          "confidence": "high", "reason": "研究"}],
        trader(), knowledge_block="K", place_orders=False)
    assert ids == {"600001": 11}
    assert len(called) == 1


def test_existing_trader_state_is_resumed_not_reseeded(monkeypatch):
    cutoff = datetime(2026, 9, 25, 9, 5, tzinfo=TZ)
    previous = TraderState.create(
        trader_id="default",
        as_of=datetime(2026, 9, 24, 15, 0, tzinfo=TZ),
        market_view={"regime": "range"})
    monkeypatch.setattr(C, "_logical_now", lambda: cutoff)
    monkeypatch.setattr(C, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(C, "_panel", lambda recs, prev: fixed_panel())
    monkeypatch.setattr(C, "_portfolio_context", lambda *args: "")
    monkeypatch.setattr(
        C.trader_state_store, "load_latest",
        lambda **kwargs: previous)
    saved = []
    monkeypatch.setattr(
        C.trader_state_store, "save",
        lambda value, **kwargs: saved.append(value) or True)
    monkeypatch.setattr(C, "create_pending_order", lambda **kwargs: None)

    async def propose(**kwargs):
        return {
            "orders": [], "watch": [], "rejected": [], "refused": [],
            "parse_error": None, "decision_status": "abstained",
            "no_trade_reason": "没有合适机会", "decision_id": "capture-r",
        }

    monkeypatch.setattr(C.t1_decider, "propose", propose)
    got = asyncio.run(C.plan_morning(
        [{"code": "600001", "name": "甲", "theme": "算力"}],
        trader()))

    assert got["state_version"] == 2
    assert saved[0].parent_state_hash == previous.state_hash
    assert saved[-1].recent_decisions[-1].action == Action.HOLD


def test_risk_calendar_failure_is_fail_closed(monkeypatch):
    from alpha_agents.pipeline import scheduler as S

    monkeypatch.setattr(
        S.ak, "tool_trade_date_hist_sina",
        lambda: (_ for _ in ()).throw(RuntimeError("calendar down")))
    assert S._is_trading_day(
        datetime(2026, 10, 1, 9, 0), fail_closed=True) is False


def test_final_trader_prompt_contains_the_frozen_research_packet():
    from alpha_agents.agents import t1_decider as D

    text = D.build_message(
        day="2026-09-25", prev_day="2026-09-24",
        panel=fixed_panel(), news=[], book="", knowledge="",
        trader_note="", picks=1, template=D.load_prompt(),
        phase="open", decision_time="2026-09-25 09:05:00",
        research_packet={
            "source": "morning_research",
            "events_context": "隔夜订单超预期",
            "candidates": [{"code": "600001", "reason": "主线资金"}],
        })
    assert "隔夜订单超预期" in text
    assert "主线资金" in text
    assert "morning_research" in text


def test_logical_now_drops_wall_clock_microseconds(monkeypatch):
    monkeypatch.setattr(
        C.trader_session, "instant",
        lambda: "2026-09-25 09:05:00.742381")
    assert C._logical_now().isoformat() == "2026-09-25T09:05:00+08:00"
