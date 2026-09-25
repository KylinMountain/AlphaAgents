"""M2-C contracts: live trading uses the replay planner and fill physics."""

import asyncio
import inspect
from types import SimpleNamespace

from alpha_agents.data.t1_execution import capacity_shares
from alpha_agents.pipeline import live_t1
from alpha_agents.pipeline.tasks import book_manager
from alpha_agents.pipeline.tasks import morning_scan


def trader():
    return SimpleNamespace(
        id="default", name="Default", note="n", extra_prompt="",
        default_horizon_days=5,
    )


def test_live_plan_calls_shared_t1_decider_and_intent(monkeypatch):
    panel = [{
        "code": "600000", "name": "浦发", "close": 10.0,
        "change_pct": 1.2, "adv20": 100000.0, "turnover_rate": 2.0,
        "concepts": ["银行"], "_theme": "银行",
    }]
    called = {}
    placed = []

    monkeypatch.setattr(live_t1.clock, "today", lambda: "2026-09-25")
    monkeypatch.setattr(live_t1, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(live_t1, "_panel", lambda recs, day: panel)
    monkeypatch.setattr(live_t1, "_portfolio_context", lambda *a: "BOOK")
    monkeypatch.setattr(live_t1, "_knowledge", lambda x: "KNOWLEDGE")
    monkeypatch.setattr(live_t1, "_record",
                        lambda *a, **k: called.setdefault("recorded", True))
    monkeypatch.setattr(live_t1, "_write_thesis", lambda *a: 77)

    async def propose(**kwargs):
        called["planner"] = kwargs
        return {
            "orders": [{
                "code": "600000", "entry_low": 9.8, "entry_high": 10.2,
                "stop_loss": 9.2, "target_price": 11.5,
                "reason": "共享 planner 决定", "invalidations": [],
            }],
            "refused": [], "parse_error": None, "decision_status": "ordered",
            "frame_hash": "frame", "decision_id": 1,
        }

    monkeypatch.setattr(live_t1.t1_decider, "propose", propose)

    def submit(**kwargs):
        placed.append(kwargs)
        return 123

    monkeypatch.setattr(live_t1, "create_pending_order", submit)

    result = asyncio.run(live_t1.plan_open(
        [{"code": "600000", "name": "浦发", "theme": "银行"}],
        trader(), prediction_ids={"600000": 44},
        knowledge_block="approved", events_context="overnight",
    ))

    assert called["planner"]["origin"] == "live_morning"
    assert called["planner"]["panel"] == panel
    assert called["planner"]["book"] == "BOOK"
    assert called["planner"]["knowledge"] == "KNOWLEDGE"
    assert called["recorded"]
    assert result["placed"][0]["order_id"] == 123
    assert placed[0]["source"] == "t1_live"
    assert placed[0]["prediction_id"] == 44
    assert placed[0]["thesis_id"] == 77


def test_empty_live_panel_is_deterministic_abstention(monkeypatch):
    monkeypatch.setattr(live_t1.clock, "today", lambda: "2026-09-25")
    monkeypatch.setattr(live_t1, "_previous_session", lambda day: "2026-09-24")
    monkeypatch.setattr(live_t1, "_panel", lambda recs, day: [])

    async def forbidden(**kwargs):
        raise AssertionError("model must not be called for an empty allowed panel")

    monkeypatch.setattr(live_t1.t1_decider, "propose", forbidden)
    result = asyncio.run(live_t1.plan_open([], trader()))
    assert result["decision_status"] == "abstained"
    assert result["placed"] == []


def test_production_morning_path_does_not_use_legacy_order_writer():
    source = inspect.getsource(morning_scan._scan_for)
    assert "place_orders=False" in source
    assert "live_t1.plan_open" in source
    assert "不回退旧下单路径" in source


def test_research_prediction_writer_can_be_read_only_for_trade(monkeypatch):
    called = []
    monkeypatch.setattr(morning_scan.clock, "today", lambda: "2026-09-25")
    monkeypatch.setattr(
        morning_scan, "get_stock_quotes_fn",
        lambda **k: '{"quotes":[{"code":"600000","price":10.0}]}')
    monkeypatch.setattr(morning_scan, "build_decision_context",
                        lambda **k: {"hash": "x"})
    monkeypatch.setattr(morning_scan, "safe_active_themes", lambda: [])
    monkeypatch.setattr(morning_scan, "safe_market_regime", lambda: "")
    monkeypatch.setattr(morning_scan, "safe_sentiment_phase", lambda: "")
    monkeypatch.setattr(morning_scan, "_morning_relation_status",
                        lambda *a, **k: "not_checked")
    monkeypatch.setattr(
        morning_scan, "save_prediction",
        lambda **kwargs: called.append(kwargs) or 9)
    monkeypatch.setattr(
        morning_scan, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("research stage must not place the trade")))

    ids = morning_scan._save_recommendations_list(
        [{"code": "600000", "name": "浦发", "theme": "银行",
          "confidence": "high", "reason": "research"}],
        trader(), knowledge_block="K", place_orders=False,
    )
    assert ids == {"600000": 9}
    assert len(called) == 1


def test_live_t1_capacity_uses_completed_adv20(monkeypatch):
    monkeypatch.setattr(
        book_manager.mh, "get_latest_trading_day_at_or_before",
        lambda day: "2026-09-24")
    monkeypatch.setattr(
        book_manager.mh, "get_local_history",
        lambda code, days, as_of: [{"volume": 10000}] * 20)

    limits = book_manager._capacity_limits(
        [{"code": "600000", "source": "t1_live"},
         {"code": "600001", "source": "morning"}],
        "2026-09-25",
    )
    assert limits == {"600000": capacity_shares(10000)}


def test_missing_adv20_is_zero_not_unlimited_for_new_path(monkeypatch):
    monkeypatch.setattr(
        book_manager.mh, "get_latest_trading_day_at_or_before",
        lambda day: "2026-09-24")
    monkeypatch.setattr(book_manager.mh, "get_local_history", lambda *a, **k: None)

    assert book_manager._capacity_limits(
        [{"code": "600000", "source": "t1_live"}], "2026-09-25"
    ) == {"600000": 0}
