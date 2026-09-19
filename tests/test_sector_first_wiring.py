"""The opt-in sector-first path owns stock themes without changing incumbent defaults."""

from collections import Counter
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.agents import t1_decider  # noqa: E402
from alpha_agents.data import opportunity_journal as OJ  # noqa: E402


class _Ctx:
    selection_architecture = "sector_first_v0"
    panel_size = 40
    news_limit = 60
    trader = "pullback"
    trader_note = ""
    picks = 2
    prompt = "unused"
    model = object()
    loop = None
    max_turns = None
    participation = 0.10
    theme = "RUN-THEME"
    run_id = "r1"
    trader_tools = False
    counters = Counter()
    capacity = {}
    last_panel_candidate_pool = []
    last_sector_context = {}

    class _Corpus:
        pass

    corpus = _Corpus()


def _order():
    return {
        "code": "600001",
        "entry_low": 10.0,
        "entry_high": 10.5,
        "stop_loss": 9.5,
        "target_price": 12.0,
        "reason": "fixture",
    }


def _wire_common(monkeypatch):
    monkeypatch.setattr(wf, "_market_state", lambda *a, **k: {})
    monkeypatch.setattr(wf, "_news_window", lambda *a, **k: [])
    monkeypatch.setattr(wf, "_book_and_knowledge", lambda *a, **k: ("", ""))
    monkeypatch.setattr(wf, "_trader_tools", lambda *a, **k: [])
    monkeypatch.setattr(wf, "_event_snapshot_refs", lambda *a, **k: [])
    monkeypatch.setattr(wf, "capacity_shares", lambda *a, **k: 1000)
    monkeypatch.setattr(OJ, "record_decision", lambda **k: 1)
    monkeypatch.setattr(
        t1_decider,
        "propose_sync",
        lambda **k: {
            "orders": [_order()],
            "refused": [],
            "parse_error": None,
            "raw": "{}",
            "research_budget": None,
        },
    )


def test_sector_first_order_uses_stock_primary_theme(monkeypatch):
    ctx = _Ctx()
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage", lambda *a, **k: (panel, None))
    monkeypatch.setattr(
        wf, "_sector_stock_choice",
        lambda *a, **k: {
            "stocks": [{"code": "600001", "reason": "pick"}],
            "refused": [], "parse_error": None, "research_budget": None,
        })
    monkeypatch.setattr(
        wf, "_sector_trade_plan",
        lambda *a, **k: {
            "orders": [_order()], "refused": [], "parse_error": None,
            "raw": "{}", "research_budget": None,
        })
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        wf,
        "create_pending_order",
        lambda **kwargs: captured.update(kwargs) or 7,
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert got[0]["theme"] == "AI"
    assert got[0]["supporting_themes"] == ["算力"]
    assert captured["theme"] == "AI"
    assert captured["theme"] != ctx.theme


def test_dual_rank_default_keeps_run_theme(monkeypatch):
    ctx = _Ctx()
    ctx.selection_architecture = "dual_rank_v0"
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
    }]
    monkeypatch.setattr(wf, "_build_panel", lambda *a, **k: panel)
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        wf,
        "create_pending_order",
        lambda **kwargs: captured.update(kwargs) or 8,
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert got[0]["theme"] == "RUN-THEME"
    assert captured["theme"] == "RUN-THEME"


def test_cli_default_does_not_change_incumbent_architecture():
    args = wf.build_parser().parse_args(["--start", "2026-01-30"])
    assert args.selection_architecture == "dual_rank_v0"
    assert args.sector_membership is None


def test_sector_first_context_requires_pit_archive(tmp_path, monkeypatch):
    class _Corpus:
        def __init__(self, _data_dir):
            pass

    monkeypatch.setattr(wf, "Corpus", _Corpus)
    args = wf.build_parser().parse_args([
        "--start", "2026-01-30",
        "--decider", "llm",
        "--selection-architecture", "sector_first_v0",
    ])
    args.run_id = "x"
    with pytest.raises(SystemExit, match="sector-membership"):
        wf.Context(args)



def test_no_flow_arm_filters_direction_flow_news_only():
    ctx = _Ctx()
    ctx.selection_architecture = "sector_first_no_flow"
    news = [
        {"title": "AI 主力资金净流入 10 亿", "content": ""},
        {"title": "AI 新产品发布", "content": "订单增长"},
        {"title": "北向资金净买入", "content": "市场"},
    ]
    got = wf._direction_news(ctx, news)
    assert [row["title"] for row in got] == ["AI 新产品发布"]


def test_no_flow_arm_is_opt_in_cli_choice():
    args = wf.build_parser().parse_args([
        "--start", "2026-01-30",
        "--selection-architecture", "sector_first_no_flow",
    ])
    assert args.selection_architecture == "sector_first_no_flow"



def test_c_arm_simple_stock_choice_never_calls_llm_selector(monkeypatch):
    from alpha_agents.agents import sector_stock_selector

    ctx = _Ctx()
    ctx.selection_architecture = "sector_first_simple_selector"
    panel = [
        {"code": "600001"},
        {"code": "600002"},
        {"code": "600003"},
    ]
    monkeypatch.setattr(
        sector_stock_selector, "propose_sync",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("C must not call the LLM stock selector")),
    )
    got = wf._sector_stock_choice(
        ctx, day="2026-01-30", prev_day="2026-01-29",
        panel=panel, news=[], market={}, book="", knowledge="",
        research_budget=None)
    assert [row["code"] for row in got["stocks"]] == [
        "600001", "600002"]


def test_sector_trade_plan_is_toolless(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        t1_decider, "propose_sync",
        lambda **kwargs: captured.update(kwargs) or {
            "orders": [], "refused": [], "parse_error": None,
            "raw": "", "research_budget": None,
        })
    ctx = _Ctx()
    ctx.prompt = "plan"
    wf._sector_trade_plan(
        ctx, day="2026-01-30", prev_day="2026-01-29",
        panel=[{"code": "600001"}], news=[], market={},
        book="", knowledge="")
    assert captured["tools"] == []
    assert captured["research_budget"] is None



def test_sector_stock_card_renders_leave_one_out_peer_strength():
    panel = [{
        "code": "600001",
        "name": "甲",
        "close": 10.0,
        "change_pct": 2.0,
        "adv20": 100000,
        "turnover_rate": 3.0,
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
        "primary_theme_peer_covered": 3,
        "primary_theme_peer_total": 4,
        "primary_theme_peer_relative_5d_pct": 1.234,
    }]
    rendered = t1_decider.format_panel(panel)
    assert "主方向去自身5日相对" in rendered
    assert "+1.23% (3/4)" in rendered
